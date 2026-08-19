from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
import json
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, TypeVar
import weakref
import webbrowser

from .auto_ban import (
    AUTO_BAN_STALE_TIMER_GRACE_SECONDS,
    AUTO_BAN_TARGET_MAX_MS,
    AUTO_BAN_TARGET_MIN_MS,
    auto_ban_deadline_after_timer_sample,
    auto_ban_monitor_due,
    auto_ban_stage_due,
    choose_auto_ban_stage_lead_ms,
    choose_auto_ban_target_ms,
    projected_auto_ban_remaining_ms,
)
from .bottom_lane import analyze_bottom_lane
from .builds import BuildApplicator, BuildApplyError
from .champions import ChampionRegistry
from .codex_cli import CodexCliClient, CodexCliError, CodexTurn
from .history import (
    HistoryOverview, MatchHistoryEntry, analyze_history,
    attach_match_lp_changes, refresh_recent_20_summary,
)
from .icons import BuildAssetPreloader, ChampionIconCache, ItemIconCache, RemoteIconCache
from .i18n import (
    LANGUAGE_LABELS, RUNE_NAMES_EN, RUNE_STYLE_NAMES_EN,
    SUMMONER_SPELL_NAMES_EN, localized_text, normalize_language, translate_text,
)
from .lcu import (
    LcuActionError, LcuActionManualOverride, LcuActionStateChanged,
    LcuClient, LcuUnavailable,
    champ_select_time_left_ms, champ_select_timer_phase,
    find_local_champion_action, parse_lcu_session, session_banned_champion_ids,
)
from .live_client import LiveClient, LiveClientUnavailable
from .live_identity import (
    gameflow_puuid_by_champion, gameflow_summoner_id_by_champion,
    live_identity_available, live_identity_count, live_roster_fingerprint,
    merge_live_roster_identities, update_live_identity_payload,
)
from .models import (
    BuildAsset, BuildItemGroup, ChampionBuildGuide, DraftBan, DraftMember, DraftSnapshot,
    GamePrediction, LiveGameSnapshot, JungleTendencyStat, LaneMatchupStat,
    LivePlayer, OpggCounter, OpggMcpSummonerProfile, OpggSnapshot,
    OpggSynergySnapshot, OpggSynergyStat, PersonalStat, PlayerProfileStat,
    PlayerBehaviorStat,
    Recommendation, RuneBuild, SummonerSpellBuild,
)
from .opgg import OpggClient, OpggError
from .opgg_mcp import (
    OpggMcpClient, OpggMcpError, completed_solo_ranked_matches,
)
from .player_history import (
    OtherPlayerHistoryPager, normalize_riot_id, split_riot_id,
)
from .prompting import (
    MEMORY_PROMPT_VERSION, ResponseError, StaleResponseError,
    UnavailableRecommendationError,
    build_memory_prompt, build_prompt, parse_response,
)
from .riot_api import RiotApiClient, RiotApiError, riot_puuid_is_canonical
from .runes import RuneCatalog, RuneStyle
from .storage import Storage


T = TypeVar("T")

DUO_GROUP_COLORS = (
    "#ff4d6d",  # red-pink
    "#8be234",  # lime
    "#b678ff",  # violet
    "#28d7e6",  # cyan
    "#ffb84d",  # amber
)
DUO_LEVEL_PRIORITY = {"가능": 1, "유력": 2, "매우 유력": 3}
DuoVisual = tuple[str, str, str, str, str]

# 오래된 로컬 값은 계속 표시하고, 같은 외부 요청을 다시 허용하는
# 시점만 아래 사용자 설정으로 제어한다. Riot 개발 키 만료 시간은 별개다.
DATA_PREFERENCE_SPECS = (
    (
        "opgg_meta_cooldown_hours", "OP.GG 메타 재요청", "시간",
        24, 1, 720,
    ),
    (
        "opgg_matchup_cooldown_hours", "OP.GG 상성 재요청", "시간",
        24, 1, 720,
    ),
    (
        "opgg_build_cooldown_hours", "OP.GG 빌드 재요청", "시간",
        24, 1, 720,
    ),
    (
        "player_analysis_cooldown_hours", "플레이어 분석 재요청", "시간",
        24, 1, 720,
    ),
    (
        "opgg_synergy_cooldown_hours", "원딜×서폿 조합 재요청", "시간",
        24, 1, 720,
    ),
    (
        "local_assets_cooldown_hours", "챔피언·룬 자산 확인", "시간",
        24, 1, 720,
    ),
    (
        "opgg_meta_display_count", "OP.GG 메타 표시", "개",
        5, 1, 20,
    ),
)
DATA_PREFERENCE_LIMITS = {
    key: (default, minimum, maximum)
    for key, _label, _unit, default, minimum, maximum in DATA_PREFERENCE_SPECS
}

LUX_AUTO_BAN_FALLBACK_MIN_SECONDS = 1.4
LUX_AUTO_BAN_FALLBACK_MAX_SECONDS = 2.4
LUX_AUTO_BAN_MONITOR_INTERVAL_SECONDS = 0.12
LUX_AUTO_BAN_STATUS_INTERVAL_SECONDS = 0.25
LUX_AUTO_BAN_DISCOVERY_INTERVAL_SECONDS = 0.15
LUX_AUTO_BAN_IDLE_INTERVAL_SECONDS = 0.60

# Match-v5 is not always updated at the same moment the League client leaves
# InProgress. Retry conservatively instead of treating the first empty list as
# final. These delays are relative to the previous attempt and stay well below
# the development-key rate limits.
POST_GAME_SYNC_RETRY_DELAYS_MS = (8_000, 20_000, 45_000, 90_000)
POST_GAME_SYNC_CHECK_INTERVAL_MS = 700
LUX_AUTO_BAN_DISPLAY_INTERVAL_MS = 160
AUTO_ACCEPT_DELAY_MIN_SECONDS = 1.3
AUTO_ACCEPT_DELAY_MAX_SECONDS = 2.2
PREDICTION_SETTLE_TIMEOUT_SECONDS = 12.0
PREDICTION_SAVE_DEBOUNCE_MS = 550
BUILD_STATISTICS_SCHEMA_VERSION = 1
LIVE_IDENTITY_CACHE_SETTING = "live_roster_identity_cache_v1"
LIVE_IDENTITY_CAPTURE_FAST_SECONDS = 12.0
LIVE_IDENTITY_CAPTURE_MAX_SECONDS = 300.0

RECOMMENDATION_ACTION_SPECS = (
    ("롤에 선택", "hover", "blue"),
    ("픽 확정", "pick", "green"),
)

LOCAL_RECOMMENDATION_FALLBACKS = {
    "TOP": ("Malphite", "Ornn", "Shen"),
    "JUNGLE": ("Amumu", "Nocturne", "Warwick"),
    "MIDDLE": ("Ahri", "Orianna", "Annie"),
    "BOTTOM": ("Jinx", "Ashe", "MissFortune"),
    "SUPPORT": ("Nautilus", "Braum", "Janna"),
}


# Backward-compatible names remain importable for existing integrations and
# tests while the implementation is now champion-agnostic.
choose_lux_auto_ban_target_ms = choose_auto_ban_target_ms
choose_lux_auto_ban_stage_lead_ms = choose_auto_ban_stage_lead_ms
lux_auto_ban_monitor_due = auto_ban_monitor_due
lux_auto_ban_stage_due = auto_ban_stage_due
lux_auto_ban_deadline_after_timer_sample = auto_ban_deadline_after_timer_sample
projected_lux_auto_ban_remaining_ms = projected_auto_ban_remaining_ms


def choose_auto_accept_delay_seconds(
    picker: Callable[[float, float], float] = random.uniform,
) -> float:
    return float(picker(
        AUTO_ACCEPT_DELAY_MIN_SECONDS, AUTO_ACCEPT_DELAY_MAX_SECONDS,
    ))


def opgg_account_unavailable_error(exc: Exception) -> bool:
    """Distinguish a hidden/missing account from a transient MCP outage."""
    message = str(exc).casefold()
    return any(token in message for token in (
        "비공개", "private", "찾지 못", "not found", "존재하지",
    ))


def unavailable_player_profile() -> PlayerProfileStat:
    """A per-player terminal state that must not block the other nine cards."""
    return PlayerProfileStat(
        champion_data_source="PRIVATE_OR_UNAVAILABLE",
        sample_scope="계정 비공개 또는 조회 불가",
        status="PRIVATE_OR_UNAVAILABLE",
    )


def riot_authentication_error(exc: Exception) -> bool:
    return isinstance(exc, RiotApiError) and any(
        token in str(exc) for token in ("키가 만료", "키가 설정되지")
    )


def recent_prediction_accuracy(
    entries: list[MatchHistoryEntry], limit: int = 20,
) -> tuple[int, int, float | None]:
    """Return accuracy only inside the newest match window."""
    predicted = [
        entry for entry in entries[:max(0, int(limit))]
        if entry.prediction_correct is not None
    ]
    hits = sum(entry.prediction_correct is True for entry in predicted)
    total = len(predicted)
    return hits, total, (hits / total * 100.0 if total else None)


def exact_lp_badge_text(entry: MatchHistoryEntry) -> str:
    """Show numeric LP only when the before/after observation is exact."""
    if entry.lp_delta is None or entry.lp_confidence.upper() != "EXACT":
        return ""
    return f"{entry.lp_delta:+d} LP"


def recent_exact_lp_summary(
    entries: list[MatchHistoryEntry], limit: int = 20,
) -> tuple[int | None, int, int]:
    """Return exact LP sum, observed games, and recent window size."""
    recent = entries[:max(0, int(limit))]
    deltas = [
        entry.lp_delta for entry in recent
        if entry.lp_delta is not None and entry.lp_confidence.upper() == "EXACT"
    ]
    return (sum(deltas) if deltas else None, len(deltas), len(recent))


def cache_manager_champion_ids(
    position_ids: set[str],
    meta_ids: set[str],
    build_ids: set[str],
    matchup_ids: set[str],
) -> set[str]:
    """Keep stale caches from assigning champions to the wrong role.

    A saved build or matchup remains useful as a stale local value, but it is
    not evidence that the champion still belongs to that OP.GG position. The
    current position catalog is authoritative whenever it is available.
    """
    if position_ids:
        return set(position_ids)
    return set(meta_ids) | set(build_ids) | set(matchup_ids)


def live_roster_signature(snapshot: LiveGameSnapshot) -> str:
    """Identify changes that require reloading ten-player data."""
    return repr(tuple(sorted(
        (
            player.riot_id.casefold(), player.champion_id, player.team,
            player.position, player.draft_pick_turn,
            player.draft_team_pick_order,
        )
        for player in snapshot.players
    )))


def live_active_context_signature(snapshot: LiveGameSnapshot) -> str:
    """Identify active-player/team changes without reloading every profile."""
    return repr((
        snapshot.active_team,
        snapshot.active_riot_id.casefold(),
        tuple(sorted(
            (player.riot_id.casefold(), player.is_active_player)
            for player in snapshot.players
        )),
    ))

COLORS = {
    "bg": "#070b13",
    "panel": "#0f1726",
    "panel_2": "#151f32",
    "border": "#263754",
    "text": "#e8eefc",
    "muted": "#94a3bd",
    "gold": "#e6bd61",
    "blue": "#55b3ff",
    "green": "#48dda0",
    "purple": "#be8cff",
    "red": "#ff6b7c",
    "orange": "#f5a95e",
    "chip": "#1b2941",
    "surface": "#101a2b",
    "surface_hover": "#182740",
    "surface_selected": "#203b60",
    "divider": "#1d2a40",
}

BUTTON_FILLS = {
    COLORS["blue"]: "#174667",
    COLORS["green"]: "#17513e",
    COLORS["purple"]: "#463064",
    COLORS["gold"]: "#584720",
    COLORS["orange"]: "#57371e",
    COLORS["red"]: "#572633",
    COLORS["muted"]: "#2a3548",
}


def history_result_style(won: bool) -> tuple[str, str, str]:
    """Return accent, card tint, and badge tint for a match result."""
    if won:
        return COLORS["green"], "#0d2827", "#17483c"
    return COLORS["red"], "#2a151e", "#4a202d"

RANK_COLORS = {
    "IRON": "#8c8a87", "BRONZE": "#b8794c", "SILVER": "#b8c4cf",
    "GOLD": COLORS["gold"], "PLATINUM": "#54d6c0", "EMERALD": "#42d68a",
    "DIAMOND": "#72a8ff", "MASTER": COLORS["purple"],
    "GRANDMASTER": COLORS["red"], "CHALLENGER": "#62d7ff",
}

ROLE_LABELS = {
    "TOP": "TOP", "JUNGLE": "JGL", "MIDDLE": "MID", "BOTTOM": "ADC",
    "SUPPORT": "SUP", "UTILITY": "SUP", "UNKNOWN": "?",
}

POSITION_GLYPHS = {
    "TOP": "⬒", "JUNGLE": "♧", "MIDDLE": "◆", "BOTTOM": "➹",
    "SUPPORT": "✦", "UTILITY": "✦", "UNKNOWN": "?",
}

POSITION_BADGE_COLORS = {
    "TOP": COLORS["orange"], "JUNGLE": COLORS["green"],
    "MIDDLE": COLORS["purple"], "BOTTOM": COLORS["blue"],
    "SUPPORT": COLORS["gold"], "UTILITY": COLORS["gold"],
    "UNKNOWN": COLORS["muted"],
}

SUMMONER_SPELLS: dict[int, tuple[str, str]] = {
    1: ("정화", "SummonerBoost.png"),
    3: ("탈진", "SummonerExhaust.png"),
    4: ("점멸", "SummonerFlash.png"),
    6: ("유체화", "SummonerHaste.png"),
    7: ("회복", "SummonerHeal.png"),
    11: ("강타", "SummonerSmite.png"),
    12: ("순간이동", "SummonerTeleport.png"),
    13: ("총명", "SummonerMana.png"),
    14: ("점화", "SummonerDot.png"),
    21: ("방어막", "SummonerBarrier.png"),
    32: ("표식", "SummonerSnowball.png"),
}

POSITION_NAMES = {
    "TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜",
    "SUPPORT": "서포터", "UTILITY": "서포터", "UNKNOWN": "포지션 미정",
}

CACHE_POSITION_CHOICES = (
    ("TOP", "탑 (TOP)"),
    ("JUNGLE", "정글 (JGL)"),
    ("MIDDLE", "미드 (MID)"),
    ("BOTTOM", "원딜 (ADC)"),
    ("SUPPORT", "서포터 (SUP)"),
)

SUPPORT_ARCHETYPES = {
    "UTILITY": {
        "Janna", "Karma", "Lulu", "Milio", "Nami", "Renata", "Senna",
        "Seraphine", "Sona", "Soraka", "Taric", "Yuumi", "Zilean",
    },
    "ENGAGE": {
        "Alistar", "Blitzcrank", "Braum", "Leona", "Maokai", "Nautilus",
        "Poppy", "Pyke", "Rakan", "Rell", "TahmKench", "Thresh",
    },
    "POKE": {
        "Ashe", "Brand", "Heimerdinger", "Lux", "Morgana", "Neeko",
        "Senna", "Shaco", "Velkoz", "Xerath", "Zyra",
    },
}

SUPPORT_FILTER_LABELS = {
    "ALL": "전체",
    "UTILITY": "유틸·안정",
    "ENGAGE": "이니시",
    "POKE": "견제·딜",
}

MANUAL_UNKNOWN_SUPPORT = "__MANUAL_UNKNOWN_SUPPORT__"
LIVE_CHAMPION_SAMPLE_MATCHES = 5
LIVE_PROFILE_DETAIL_BUDGET = 50
LIVE_TOTAL_DETAIL_BUDGET = 60

MATCHUP_RUNE_WEIGHTS: dict[str, dict[int, int]] = {
    "ENGAGE": {8465: 8, 8473: 5, 8444: 2, 8439: 2, 8345: 1},
    "POKE": {8444: 7, 8345: 6, 8465: 4, 8316: 2, 8214: 1},
    "UTILITY": {8112: 7, 8229: 7, 8214: 6, 8351: 3, 8360: 2},
}

MATCHUP_ITEM_PRIORITY: dict[str, tuple[int, ...]] = {
    "ENGAGE": (3190, 3109, 3222, 3110, 3075, 3065),
    "POKE": (3107, 3222, 6617, 3083, 6616, 3065),
    "UTILITY": (3165, 3916, 3011, 6616, 3504, 6620),
    "OTHER": (3190, 3109, 3107, 3222, 3050, 3065),
}


def _fmt_rate(value: float | None) -> str:
    return "데이터 없음" if value is None else f"{value:.1f}%"


def _fmt_games(value: int) -> str:
    return "표본 미제공" if not value else f"{value:,}게임"


def participant_row_key(participant: dict, index: int = 0) -> str:
    puuid = str(participant.get("puuid") or "").strip()
    if puuid:
        return puuid
    game_name = str(
        participant.get("riotIdGameName") or participant.get("summonerName") or ""
    ).strip()
    tag_line = str(
        participant.get("riotIdTagline") or participant.get("riotIdTagLine") or ""
    ).strip()
    return (
        f"{game_name}#{tag_line}" if game_name or tag_line
        else f"row:{index}:{participant.get('teamId')}:{participant.get('championName')}"
    )


def participant_performance_ranks(participants: list[dict]) -> dict[str, int]:
    """Return a transparent local 1-10 performance estimate for Riot match rows."""
    if not participants:
        return {}
    team_kills: dict[int, int] = {}
    for participant in participants:
        team_id = int(participant.get("teamId") or 0)
        team_kills[team_id] = team_kills.get(team_id, 0) + int(
            participant.get("kills") or 0
        )
    rows: list[tuple[str, dict[str, float], bool]] = []
    for index, participant in enumerate(participants):
        kills = int(participant.get("kills") or 0)
        deaths = int(participant.get("deaths") or 0)
        assists = int(participant.get("assists") or 0)
        team_id = int(participant.get("teamId") or 0)
        rows.append((
            participant_row_key(participant, index),
            {
                "kda": (kills + assists) / max(deaths, 1),
                "kp": (kills + assists) / max(team_kills.get(team_id, 0), 1),
                "damage": float(participant.get("totalDamageDealtToChampions") or 0),
                "vision": float(participant.get("visionScore") or 0),
                "gold": float(participant.get("goldEarned") or 0),
            },
            bool(participant.get("win")),
        ))
    fields = ("kda", "kp", "damage", "vision", "gold")
    bounds = {
        field: (
            min(values := [row[1][field] for row in rows]),
            max(values),
        )
        for field in fields
    }

    def normalized(value: float, field: str) -> float:
        low, high = bounds[field]
        return (value - low) / (high - low) if high > low else 0.5

    scored: list[tuple[float, str]] = []
    for key, values, won in rows:
        score = (
            normalized(values["kda"], "kda") * 0.25
            + normalized(values["kp"], "kp") * 0.18
            + normalized(values["damage"], "damage") * 0.24
            + normalized(values["vision"], "vision") * 0.17
            + normalized(values["gold"], "gold") * 0.11
            + float(won) * 0.05
        )
        scored.append((score, key))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return {key: rank for rank, (_score, key) in enumerate(scored, start=1)}


def _result_streak(matches: list[object], champion_key: int | None = None) -> int:
    completed = [
        match for match in matches
        if str(getattr(match, "game_type", "")).upper() == "SOLORANKED"
        and str(getattr(match, "result", "")).upper() in {"WIN", "LOSE"}
        and (
            champion_key is None
            or int(getattr(match, "champion_key", 0)) == int(champion_key)
        )
    ]
    if not completed:
        return 0
    first = str(getattr(completed[0], "result", "")).upper()
    count = 0
    for match in completed:
        if str(getattr(match, "result", "")).upper() != first:
            break
        count += 1
    return count if first == "WIN" else -count


def opgg_recent_form(
    profile: OpggMcpSummonerProfile, champion_key: int
) -> dict[str, int | float | str | bool | None]:
    completed = [
        match for match in profile.recent_matches
        if match.game_type.upper() == "SOLORANKED"
        and match.result.upper() in {"WIN", "LOSE"}
    ]
    if any(match.created_at for match in completed):
        completed.sort(key=lambda match: match.created_at or "", reverse=True)
    recent = completed[:10]
    champion_recent = [
        match for match in completed if int(match.champion_key) == int(champion_key)
    ][:10]
    scored = [match.op_score for match in recent if match.op_score > 0]
    last_game = recent[0] if recent else None
    return {
        "recent_games": len(recent),
        "recent_wins": sum(match.result.upper() == "WIN" for match in recent),
        "recent_kills": sum(match.kills for match in recent),
        "recent_deaths": sum(match.deaths for match in recent),
        "recent_assists": sum(match.assists for match in recent),
        "recent_op_score": sum(scored) / len(scored) if scored else 0.0,
        # These fields deliberately come from the same completed solo-ranked
        # match.  Mixing a locally cached KDA with an OP.GG rank from another
        # match made the previous-game signal internally inconsistent.
        "last_op_score_rank": last_game.op_score_rank if last_game else 0,
        "last_game_champion_key": last_game.champion_key if last_game else 0,
        "last_game_position": last_game.position if last_game else "UNKNOWN",
        "last_game_kills": last_game.kills if last_game else 0,
        "last_game_deaths": last_game.deaths if last_game else 0,
        "last_game_assists": last_game.assists if last_game else 0,
        "last_game_won": (
            last_game.result.upper() == "WIN" if last_game else None
        ),
        "overall_streak": _result_streak(completed),
        "champion_recent_games": len(champion_recent),
        "champion_recent_wins": sum(
            match.result.upper() == "WIN" for match in champion_recent
        ),
        "champion_streak": _result_streak(completed, champion_key),
    }


def opgg_player_history_matches(
    profile: OpggMcpSummonerProfile,
    limit: int = 10,
) -> list[object]:
    """Return a stable newest-first OP.GG fallback for a private Riot ID.

    OP.GG's recent-match tool has no cursor and exposes at most twenty rows.
    The player-history fallback deliberately shows only the first ten
    completed solo-ranked games instead of pretending it has Riot Match-v5
    items, runes, or full-team details.
    """
    matches = completed_solo_ranked_matches(list(profile.recent_matches))
    if any(match.created_at for match in matches):
        matches.sort(key=lambda match: match.created_at or "", reverse=True)
    return matches[:max(0, min(int(limit), 10))]


def opgg_jungle_tendency(
    profile: OpggMcpSummonerProfile,
    champion_key: int,
    champion_id: str = "",
    puuid: str = "",
) -> JungleTendencyStat | None:
    """Build an honest fallback from another player's OP.GG recent games.

    OP.GG supplies result and KDA, but not the Match-v5 challenge fields used
    for early ganks, ten-minute jungle CS, invades, or spawn objectives. Keep
    those fields empty instead of inventing route tendencies while still
    showing the remote player's available jungle form.
    """
    completed = [
        match for match in profile.recent_matches
        if str(match.game_type).upper() == "SOLORANKED"
        and str(match.result).upper() in {"WIN", "LOSE"}
        and str(match.position).upper() in {"JGL", "JUNGLE"}
    ]
    if any(match.created_at for match in completed):
        completed.sort(key=lambda match: match.created_at or "", reverse=True)
    completed = completed[:10]
    champion_matches = [
        match for match in completed
        if champion_key > 0 and int(match.champion_key) == int(champion_key)
    ]
    selected = champion_matches if len(champion_matches) >= 3 else completed
    if not selected:
        return None
    games = len(selected)
    wins = sum(str(match.result).upper() == "WIN" for match in selected)
    kills = sum(int(match.kills) for match in selected)
    deaths = sum(int(match.deaths) for match in selected)
    assists = sum(int(match.assists) for match in selected)
    win_rate = wins / games * 100.0
    kda = (kills + assists) / max(deaths, 1)
    labels: list[str] = []
    if games >= 3 and win_rate >= 60.0:
        labels.append("최근 정글 폼 우세")
    elif games >= 3 and win_rate <= 40.0:
        labels.append("최근 정글 폼 부진")
    if games >= 3 and kda >= 3.0:
        labels.append("최근 KDA 안정")
    return JungleTendencyStat(
        puuid=puuid,
        champion_id=champion_id,
        games=games,
        champion_specific=selected is champion_matches,
        wins=wins,
        kills=kills,
        deaths=deaths,
        assists=assists,
        labels=labels,
        status="SUMMARY",
        message="OP.GG 최근 솔로랭크 정글 요약",
    )


def jungle_tendency_advice(
    stat: JungleTendencyStat,
    ally: bool,
) -> list[str]:
    """Turn jungle evidence into conservative, side-aware game advice.

    Detailed Riot samples may support route, invade, and objective guidance.
    OP.GG summaries contain only recent result/KDA data, so they explicitly
    avoid inventing pathing tendencies.
    """
    labels = set(stat.labels)
    advice: list[str] = []
    if stat.status == "SUMMARY":
        if "최근 정글 폼 우세" in labels:
            advice.append(
                "최근 폼 우세 · 교전 호응을 기대할 수 있지만 동선은 시야로 확인하세요."
                if ally else
                "최근 폼 우세 · 소규모 교전을 길게 열지 말고 먼저 인원수를 확인하세요."
            )
        elif "최근 정글 폼 부진" in labels:
            advice.append(
                "최근 폼 부진 · 정글에게 복구 시간을 주고 불필요한 강가 교전을 줄이세요."
                if ally else
                "최근 폼 부진 · 시야가 확보되면 안전하게 주도권을 굴릴 여지가 있습니다."
            )
        else:
            advice.append(
                "최근 결과·KDA만 확인됨 · 실제 갱 동선은 미니맵과 와드로 판단하세요."
            )
        advice.append(
            "OP.GG 요약 표본이라 갱 방향·카정·오브젝트 성향은 확정하지 않습니다."
        )
        return advice

    if "갱킹 자주 감" in labels:
        advice.append(
            "초반 개입형 · 라인을 당겨 갱 공간을 만들고 핑이 오면 먼저 호응하세요."
            if ally else
            "초반 개입형 · 첫 귀환 전 강가·삼거리 시야를 두고 긴 라인을 피하세요."
        )
    elif "초반 갱 적음" in labels:
        advice.append(
            "성장 우선형 · 초반에는 정글 도움 없이 버틸 파동을 만들고 무리한 교전을 피하세요."
            if ally else
            "초반 개입이 낮음 · 시야가 확인되면 라인 주도권을 밀되 역갱 여지는 남기세요."
        )
    elif "풀캠·성장 우선" in labels:
        advice.append(
            "풀캠 성향 · 캠프가 끝나는 바위게·첫 궁극기 타이밍에 맞춰 교전을 여세요."
            if ally else
            "풀캠 성향 · 첫 풀캠 직후 강가와 6레벨 전후 개입을 특히 확인하세요."
        )
    else:
        advice.append(
            "균형형 표본 · 확정 동선보다 라인 주도권과 현재 시야를 기준으로 판단하세요."
        )

    if "카정 잦음" in labels:
        advice.append(
            "카정 성향 · 인접 라인이 먼저 밀고 강가에 합류하면 정글 격차를 키울 수 있습니다."
            if ally else
            "카정 성향 · 아군 정글 입구 와드와 미드·서폿의 선합류가 필요합니다."
        )
    elif "오브젝트 즉시" in labels:
        advice.append(
            "오브젝트 우선 · 출현 40초 전에 라인을 정리하고 강가 시야를 함께 여세요."
            if ally else
            "오브젝트 우선 · 출현 전에 귀환하고 입구 시야를 먼저 지우세요."
        )

    if {"퍼블을 자주 땀", "퍼블 관여 높음"} & labels:
        advice.append(
            "퍼블 관여 높음 · 첫 강가 교전과 2~4레벨 라인 합류에 빠르게 반응하세요."
            if ally else
            "퍼블 관여 높음 · 2~4레벨에는 체력 교환을 짧게 하고 적 위치부터 확인하세요."
        )
    elif "데스 주의" in labels:
        advice.append(
            "데스 표본 높음 · 무리한 진입을 따라가기보다 퇴로와 다음 오브젝트를 지키세요."
            if ally else
            "데스 표본 높음 · 시야 안에서 길게 받아치면 실수를 유도할 수 있습니다."
        )
    elif "생존 안정" in labels:
        advice.append(
            "생존 안정형 · 억지 추격보다 다음 캠프와 오브젝트로 이득을 이어갈 가능성이 큽니다."
        )
    return advice[:3]


def riot_local_recent_form(
    matches: list[dict], puuid: str, champion_id: str,
) -> dict[str, int | str | bool | None]:
    """Calculate the active player's streak from cached Riot Solo Queue games.

    Riot match payloads are authoritative for the local account and can be
    newer than the OP.GG profile cache.  Only the latest ten completed Solo
    Queue games are considered, newest first.
    """
    samples: list[dict] = []
    for match in sorted(
        matches,
        key=lambda item: int((item.get("info") or {}).get("gameCreation") or 0),
        reverse=True,
    ):
        info = match.get("info") or {}
        if int(info.get("queueId") or 0) != 420:
            continue
        participant = next(
            (
                item for item in (info.get("participants") or [])
                if str(item.get("puuid") or "") == puuid
            ),
            None,
        )
        if not participant:
            continue
        samples.append(participant)
        if len(samples) >= 10:
            break

    def streak(rows: list[dict]) -> int:
        if not rows:
            return 0
        first = bool(rows[0].get("win"))
        count = 0
        for row in rows:
            if bool(row.get("win")) != first:
                break
            count += 1
        return count if first else -count

    champion_samples = [
        item for item in samples
        if str(item.get("championName") or "") == champion_id
    ]
    last = samples[0] if samples else {}
    return {
        "recent_games": len(samples),
        "recent_wins": sum(bool(item.get("win")) for item in samples),
        "recent_kills": sum(int(item.get("kills") or 0) for item in samples),
        "recent_deaths": sum(int(item.get("deaths") or 0) for item in samples),
        "recent_assists": sum(int(item.get("assists") or 0) for item in samples),
        "overall_streak": streak(samples),
        "champion_recent_games": len(champion_samples),
        "champion_recent_wins": sum(
            bool(item.get("win")) for item in champion_samples
        ),
        "champion_streak": streak(champion_samples),
        "last_game_champion_id": str(last.get("championName") or ""),
        "last_game_position": str(
            last.get("teamPosition") or last.get("individualPosition")
            or "UNKNOWN"
        ),
        "last_game_kills": int(last.get("kills") or 0),
        "last_game_deaths": int(last.get("deaths") or 0),
        "last_game_assists": int(last.get("assists") or 0),
        "last_game_won": bool(last.get("win")) if last else None,
    }


def streak_badge_text(value: int, prefix: str = "") -> str:
    """Return a compact Korean streak label, hiding one-game non-streaks."""
    if abs(value) < 2:
        return ""
    count = f"{abs(value)}+" if abs(value) >= 10 else str(abs(value))
    result = "연승" if value > 0 else "연패"
    return f"{prefix}{count}{result} 중"


def allied_adc_member(draft: DraftSnapshot) -> DraftMember | None:
    members = draft.ally_team_order or [
        *draft.ally_locked, *draft.ally_hover,
        *([draft.my_hover] if draft.my_hover else []),
    ]
    candidates = [
        member for member in members
        if member.role == "BOTTOM" and member.champion_id
        and member.state in {"LOCKED", "HOVER"}
    ]
    return next((member for member in candidates if member.state == "LOCKED"), None) or (
        candidates[0] if candidates else None
    )


def local_draft_selection(draft: DraftSnapshot) -> DraftMember | None:
    """Return the local player's current intent or locked champion.

    `my_hover` is deliberately separate in the draft model, while a completed
    local pick lives in `ally_locked`.  UI features that describe "my current
    champion" need both states and should prefer the live HOVER when present.
    """
    if draft.my_hover and draft.my_hover.champion_id:
        return draft.my_hover
    local_cell = draft.local_player_cell_id
    if local_cell is None:
        return None
    members = draft.ally_team_order or draft.ally_locked
    return next(
        (
            member for member in members
            if member.cell_id == local_cell and member.champion_id
            and member.state in {"HOVER", "LOCKED"}
        ),
        None,
    )


def recommendation_draft_context_signature(draft: DraftSnapshot) -> str:
    """Hash only draft facts that can change a champion recommendation.

    In-progress ban HOVER actions are intentionally excluded.  They should
    update the small ban strip, but only a completed ban changes champion
    availability and makes a recommendation stale.
    """
    def member_value(
        member: DraftMember, *, local_player: bool = False,
    ) -> tuple[object, ...]:
        if local_player:
            # Keep the last Codex answer visible while the user hovers, locks,
            # or swaps their own champion. Only a new Codex response replaces
            # the three recommendation cards.
            return (
                "LOCAL", member.role, member.cell_id,
                member.pick_order, member.pick_turn,
            )
        return (
            member.champion_id, member.role, member.state, member.cell_id,
            member.pick_order, member.pick_turn,
        )

    ally_members = draft.ally_team_order or [
        *draft.ally_locked, *draft.ally_hover,
        *([draft.my_hover] if draft.my_hover else []),
    ]
    enemy_members = draft.enemy_team_order or draft.enemy_locked
    payload = (
        draft.my_role,
        draft.my_pick_order,
        draft.local_player_cell_id,
        tuple(
            member_value(
                member,
                local_player=(
                    member is draft.my_hover
                    or (
                        draft.local_player_cell_id is not None
                        and member.cell_id == draft.local_player_cell_id
                    )
                ),
            )
            for member in ally_members
        ),
        tuple(member_value(member) for member in enemy_members),
        tuple(draft.ally_bans),
        tuple(draft.enemy_bans),
        draft.selected_enemy_support_id,
        draft.selected_enemy_support_source,
    )
    return sha256(repr(payload).encode("utf-8")).hexdigest()[:20]


def recommendation_action_available(
    action: str, *, enabled: bool, stale: bool, demo: bool,
) -> bool:
    """Staleness is informational; every explicit action rechecks live LCU."""
    _ = action, stale
    return bool(enabled and not demo)


def synergy_tier_label(value: int) -> str:
    return {0: "OP", 1: "S", 2: "A", 3: "B", 4: "C"}.get(value, "-")


def adc_flow_hint(champion_id: str) -> str:
    if champion_id in {"Samira", "Nilah", "Kalista", "Draven", "Tristana"}:
        return "진입·킬각 연계 우선"
    if champion_id in {"Caitlyn", "Ezreal", "Varus", "Jhin", "Ashe", "MissFortune"}:
        return "사거리 압박·포킹·CC 연계"
    if champion_id in {"Jinx", "KogMaw", "Twitch", "Aphelios", "Zeri", "Sivir"}:
        return "성장 보조·보호·한타 지속딜"
    if champion_id in {"Kaisa", "Xayah", "Lucian"}:
        return "라인 주도권과 교전 보조 균형"
    return "OP.GG 표본과 전체 조합을 함께 판단"


def opgg_synergy_snapshot_fresh(
    snapshot: OpggSynergySnapshot,
    now: datetime | None = None,
    hours: int = 24,
) -> bool:
    if not snapshot.fetched_at:
        return False
    try:
        fetched_at = datetime.fromisoformat(snapshot.fetched_at)
    except ValueError:
        return False
    return (now or datetime.now()) - fetched_at <= timedelta(hours=hours)


def support_archetype(champion_id: str) -> str:
    """Return the primary UI archetype for a support champion."""
    for archetype in ("UTILITY", "ENGAGE", "POKE"):
        if champion_id in SUPPORT_ARCHETYPES[archetype]:
            return archetype
    return "OTHER"


def position_name(position: str) -> str:
    return POSITION_NAMES.get(str(position or "SUPPORT").upper(), "서포터")


def lane_matchup_label(win_rate: float | None) -> str:
    if win_rate is None:
        return "데이터 없음"
    if win_rate >= 55.0:
        return "카운터 우위"
    if win_rate >= 51.5:
        return "유리 상성"
    if win_rate >= 48.5:
        return "반반 상성"
    if win_rate > 45.0:
        return "불리 상성"
    return "카운터 열세"


def behavior_strength_signals(
    stat: PlayerBehaviorStat | None, position: str,
) -> list[str]:
    """Turn recent solo-queue evidence into short, non-speculative strengths."""
    if not stat or stat.games < 3:
        return [f"표본 {stat.games if stat else 0}경기 · 강점 판단 보류"]
    signals: list[str] = []
    if stat.first_blood_rate is not None and stat.first_blood_rate >= 25.0:
        signals.append(f"선취점 관여 {stat.first_blood_rate:.0f}% · 초반 교전 적극")
    if stat.early_advantage_rate is not None and stat.early_advantage_rate >= 55.0:
        signals.append(f"초반 라인 우위 {stat.early_advantage_rate:.0f}% · 주도권 강점")
    if stat.kill_participation is not None and stat.kill_participation >= 65.0:
        signals.append(f"킬 관여 {stat.kill_participation:.0f}% · 합류 적극")
    vision_threshold = 1.7 if position == "SUPPORT" else 1.0
    if stat.vision_per_minute is not None and stat.vision_per_minute >= vision_threshold:
        signals.append(f"분당 시야 {stat.vision_per_minute:.2f} · 시야 투자 높음")
    if stat.games >= 5 and stat.average_deaths is not None and stat.average_deaths <= 4.0:
        signals.append(f"평균 데스 {stat.average_deaths:.1f} · 생존 안정")
    return signals or ["뚜렷한 고위험 강점 신호 없음"]


def behavior_weakness_signals(
    stat: PlayerBehaviorStat | None, position: str,
) -> list[str]:
    """Return actionable gaps only when the local sample is large enough."""
    if not stat or stat.games < 3:
        return [f"표본 {stat.games if stat else 0}경기 · 약점 단정 보류"]
    signals: list[str] = []
    if stat.average_deaths is not None and stat.average_deaths >= 6.0:
        signals.append(f"평균 데스 {stat.average_deaths:.1f} · 진입 후 이탈 빈틈")
    if stat.early_advantage_rate is not None and stat.early_advantage_rate <= 40.0:
        signals.append(f"초반 우위 {stat.early_advantage_rate:.0f}% · 라인 압박 가능")
    if stat.kill_participation is not None and stat.kill_participation <= 50.0:
        signals.append(f"킬 관여 {stat.kill_participation:.0f}% · 반대편 합류 때 수적 우위")
    if (
        position == "SUPPORT" and stat.vision_per_minute is not None
        and stat.vision_per_minute < 1.3
    ):
        signals.append(f"분당 시야 {stat.vision_per_minute:.2f} · 시야 공백 공략")
    if (
        position == "SUPPORT" and stat.control_wards is not None
        and stat.control_wards < 2.5
    ):
        signals.append(f"제어 와드 {stat.control_wards:.1f}개 · 오브젝트 시야 압박")
    return signals or ["최근 표본에서 뚜렷한 약점 신호 없음"]


def lane_matchup_from_snapshot(
    position: str,
    ally_champion_id: str,
    ally_champion_name_ko: str,
    enemy_champion_id: str,
    enemy_champion_name_ko: str,
    snapshot: OpggSnapshot,
    *,
    cached: bool,
) -> LaneMatchupStat:
    entries = [*snapshot.counters, *snapshot.weak_picks]
    counter = next(
        (entry for entry in entries if entry.champion_id == ally_champion_id), None
    )
    if ally_champion_id == enemy_champion_id:
        win_rate, games = 50.0, 0
    elif counter:
        win_rate, games = counter.versus_win_rate, counter.games
    else:
        win_rate, games = None, 0
    return LaneMatchupStat(
        position=position,
        ally_champion_id=ally_champion_id,
        ally_champion_name_ko=ally_champion_name_ko,
        enemy_champion_id=enemy_champion_id,
        enemy_champion_name_ko=enemy_champion_name_ko,
        ally_win_rate=win_rate,
        ally_laning_win_rate=(counter.laning_win_rate if counter else None),
        games=games,
        patch=snapshot.patch,
        updated_at=snapshot.updated_at,
        status=("CACHE" if cached else "OK") if win_rate is not None else "NO_DATA",
        cached=cached,
        message="" if win_rate is not None else "OP.GG 맞대결 표에 없음",
    )


def matchup_counter_for_candidate(
    snapshot: OpggSnapshot | None, champion_id: str | None,
) -> OpggCounter | None:
    """Return the candidate's lane result from an enemy-centric matchup table."""
    if not snapshot or not champion_id:
        return None
    return next(
        (
            entry for entry in [*snapshot.counters, *snapshot.weak_picks]
            if entry.champion_id == champion_id
        ),
        None,
    )


def lane_matchup_snapshot_fresh(
    snapshot: OpggSnapshot, now: datetime | None = None, hours: int = 24,
) -> bool:
    if not snapshot.updated_at:
        return False
    try:
        updated = datetime.fromisoformat(snapshot.updated_at)
    except (TypeError, ValueError):
        return False
    current = now or datetime.now(updated.tzinfo)
    if current.tzinfo is None and updated.tzinfo is not None:
        current = current.replace(tzinfo=updated.tzinfo)
    elif current.tzinfo is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=current.tzinfo)
    age = current - updated
    return timedelta(0) <= age <= timedelta(hours=hours)


def live_game_prediction_key(snapshot: LiveGameSnapshot) -> str:
    if not snapshot.players:
        return ""
    roster = sorted(
        (
            player.team, player.riot_id.casefold(), player.champion_id,
            player.is_active_player,
        )
        for player in snapshot.players
    )
    raw = json.dumps(
        {"active_team": snapshot.active_team, "roster": roster},
        ensure_ascii=False, sort_keys=True,
    )
    return sha256(raw.encode("utf-8")).hexdigest()[:24]


def game_prediction_display_signature(
    prediction: GamePrediction | None,
) -> tuple[object, ...] | None:
    """Return only values that can change the visible prediction panel.

    `captured_at` is deliberately excluded.  Live polling creates a fresh
    prediction timestamp every few seconds, but that timestamp is not shown;
    treating it as UI data caused the whole analysis section to flash.
    """
    if prediction is None:
        return None
    return (
        prediction.prediction_key,
        prediction.win_probability,
        prediction.predicted_win,
        prediction.confidence,
        prediction.evidence,
        prediction.evidence_score,
    )


def duo_group_visuals(
    players: list[LivePlayer],
    duo_pairs: dict[str, list[tuple[str, str, str]]],
    *,
    active_team: str | None = None,
) -> dict[str, DuoVisual]:
    """Choose disjoint duo pairs and give each pair one visible identity.

    Riot evidence is stored as a symmetric graph. A player can occasionally
    have several weak edges, but a premade duo is a pair, so stronger edges are
    selected first and every player appears in at most one visual group. Color
    is never the only cue: the shared A-E label and partner name are returned
    with it for color-blind accessibility.
    """
    position_order = {
        "TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3,
        "UTILITY": 4, "SUPPORT": 4, "UNKNOWN": 9,
    }
    stable_players = sorted(
        players,
        key=lambda player: (
            0 if active_team and player.team == active_team else 1,
            player.team,
            position_order.get(player.position, 9),
            player.riot_id.casefold(),
        ),
    )
    roster_by_key = {
        player.riot_id.casefold(): player for player in stable_players
    }
    roster_index = {
        player.riot_id.casefold(): index
        for index, player in enumerate(stable_players)
    }
    team_by_id = {
        player.riot_id.casefold(): player.team for player in stable_players
    }
    edges: dict[tuple[str, str], tuple[str, str]] = {}
    for first, values in duo_pairs.items():
        first_key = first.casefold()
        if first_key not in roster_index:
            continue
        for other, level, evidence in values:
            other_key = other.casefold()
            if (
                other_key not in roster_index
                or first_key == other_key
                or team_by_id.get(first_key) != team_by_id.get(other_key)
                or level not in DUO_LEVEL_PRIORITY
            ):
                continue
            pair = tuple(sorted((first_key, other_key)))
            current = edges.get(pair)
            if (
                current is None
                or DUO_LEVEL_PRIORITY[level] > DUO_LEVEL_PRIORITY[current[0]]
                or (
                    DUO_LEVEL_PRIORITY[level] == DUO_LEVEL_PRIORITY[current[0]]
                    and evidence < current[1]
                )
            ):
                edges[pair] = (level, evidence)

    ranked = sorted(
        edges.items(),
        key=lambda item: (
            -DUO_LEVEL_PRIORITY[item[1][0]],
            min(roster_index[item[0][0]], roster_index[item[0][1]]),
            max(roster_index[item[0][0]], roster_index[item[0][1]]),
            item[0],
        ),
    )
    used: set[str] = set()
    selected: list[tuple[tuple[str, str], str, str]] = []
    for pair, (level, evidence) in ranked:
        if pair[0] in used or pair[1] in used:
            continue
        used.update(pair)
        selected.append((pair, level, evidence))
    selected.sort(
        key=lambda item: min(
            roster_index[item[0][0]], roster_index[item[0][1]]
        )
    )

    result: dict[str, DuoVisual] = {}
    for index, (pair, level, evidence) in enumerate(selected):
        group_label = chr(ord("A") + index)
        color = DUO_GROUP_COLORS[index % len(DUO_GROUP_COLORS)]
        first_key, second_key = pair
        first = roster_by_key[first_key].riot_id
        second = roster_by_key[second_key].riot_id
        result[first] = (group_label, color, second, level, evidence)
        result[second] = (group_label, color, first, level, evidence)
    return result


def estimate_live_game_prediction(
    snapshot: LiveGameSnapshot,
    profiles: dict[str, PlayerProfileStat],
    lane_matchups: dict[str, LaneMatchupStat],
    duo_pairs: dict[str, list[tuple[str, str, str]]],
    *,
    captured_at: datetime | None = None,
) -> GamePrediction:
    """Estimate a restrained pre-game win chance from cached, explainable inputs."""
    active = next(
        (player for player in snapshot.players if player.is_active_player), None
    )
    if active is None and snapshot.active_riot_id:
        active = next(
            (
                player for player in snapshot.players
                if player.riot_id.casefold() == snapshot.active_riot_id.casefold()
            ),
            None,
        )
    active = active or (snapshot.allies[0] if snapshot.allies else None)

    def clamp(value: float, low: float, high: float) -> float:
        return max(low, min(value, high))

    valid_statuses = {"OK", "LOCAL_ONLY", "PARTIAL"}

    def profile_rates(
        players: list[LivePlayer], scope: str,
    ) -> list[float]:
        rates: list[float] = []
        for player in players:
            profile = profiles.get(player.riot_id)
            if not profile or profile.status not in valid_statuses:
                continue
            if scope == "season":
                games = profile.season_wins + profile.season_losses
                wins, prior = profile.season_wins, 20
            elif scope == "recent":
                games = profile.recent_games
                wins, prior = profile.recent_wins, 10
            else:
                games = profile.champion_games
                wins, prior = profile.champion_wins, 15
            if games:
                rates.append((wins + prior / 2) / (games + prior) * 100.0)
        return rates

    factor_rows: list[tuple[str, float, str]] = []

    def add_rate_factor(
        label: str, scope: str, weight: float, maximum: float,
    ) -> tuple[int, int]:
        ally_values = profile_rates(snapshot.allies, scope)
        enemy_values = profile_rates(snapshot.enemies, scope)
        if ally_values and enemy_values:
            ally_average = sum(ally_values) / len(ally_values)
            enemy_average = sum(enemy_values) / len(enemy_values)
            paired_coverage = min(len(ally_values), len(enemy_values), 5) / 5.0
            contribution = clamp(
                (ally_average - enemy_average) * weight, -maximum, maximum
            ) * paired_coverage
            factor_rows.append((
                label, contribution,
                (
                    f"{label} 아군 {ally_average:.1f}% · 적군 {enemy_average:.1f}%"
                    f" · 양팀 {min(len(ally_values), len(enemy_values))}명"
                ),
            ))
        return len(ally_values), len(enemy_values)

    season_counts = add_rate_factor("시즌", "season", 0.55, 5.0)
    champion_counts = add_rate_factor("현 챔프", "champion", 0.32, 4.0)

    position_aliases = {
        "MID": "MIDDLE", "MIDDLE": "MIDDLE",
        "ADC": "BOTTOM", "BOTTOM": "BOTTOM",
        "SUP": "SUPPORT", "UTILITY": "SUPPORT", "SUPPORT": "SUPPORT",
        "JGL": "JUNGLE", "JUNGLE": "JUNGLE", "TOP": "TOP",
    }

    def profiles_by_position(players: list[LivePlayer]) -> dict[str, PlayerProfileStat]:
        positioned: dict[str, PlayerProfileStat] = {}
        for player in players:
            position = position_aliases.get(player.position.upper(), "")
            profile = profiles.get(player.riot_id)
            if (
                position and position not in positioned and profile
                and profile.status in valid_statuses
            ):
                positioned[position] = profile
        return positioned

    ally_positioned = profiles_by_position(snapshot.allies)
    enemy_positioned = profiles_by_position(snapshot.enemies)
    profile_pairs = [
        (ally_positioned[position], enemy_positioned[position])
        for position in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")
        if position in ally_positioned and position in enemy_positioned
    ]

    def covered_contribution(
        differences: list[float], weight: float, maximum: float,
    ) -> float:
        if not differences:
            return 0.0
        full_sample = clamp(
            sum(differences) / len(differences) * weight,
            -maximum,
            maximum,
        )
        return full_sample * min(len(differences), 5) / 5.0

    recent_rate_pairs: list[tuple[float, float]] = []
    recent_kda_pairs: list[tuple[float, float]] = []
    streak_pairs: list[tuple[float, float]] = []
    last_form_pairs: list[tuple[float, float]] = []

    def last_game_form(profile: PlayerProfileStat) -> float:
        kda_score = clamp(((profile.last_game_kda or 0.0) - 2.5) / 2.5, -1.0, 1.0)
        result_score = (
            1.0 if profile.last_game_won is True
            else -1.0 if profile.last_game_won is False else 0.0
        )
        rank_score = (
            clamp((5.5 - profile.last_op_score_rank) / 4.5, -1.0, 1.0)
            if 1 <= profile.last_op_score_rank <= 10 else 0.0
        )
        return clamp(
            result_score * 0.50 + kda_score * 0.35 + rank_score * 0.15,
            -1.0,
            1.0,
        )

    for ally_profile, enemy_profile in profile_pairs:
        if ally_profile.recent_games and enemy_profile.recent_games:
            ally_rate = (
                ally_profile.recent_wins + 5
            ) / (ally_profile.recent_games + 10) * 100.0
            enemy_rate = (
                enemy_profile.recent_wins + 5
            ) / (enemy_profile.recent_games + 10) * 100.0
            recent_rate_pairs.append((ally_rate, enemy_rate))

            if (
                ally_profile.recent_games >= 3 and enemy_profile.recent_games >= 3
                and ally_profile.recent_kda is not None
                and enemy_profile.recent_kda is not None
            ):
                recent_kda_pairs.append((
                    clamp(ally_profile.recent_kda, 0.0, 8.0),
                    clamp(enemy_profile.recent_kda, 0.0, 8.0),
                ))

            def bounded_streak(profile: PlayerProfileStat) -> float:
                value = max(-4, min(profile.overall_streak, 4))
                return float(value if abs(value) >= 2 else 0)

            streak_pairs.append((
                bounded_streak(ally_profile), bounded_streak(enemy_profile),
            ))

        if (
            ally_profile.last_game_champion_id
            and enemy_profile.last_game_champion_id
        ):
            last_form_pairs.append((
                last_game_form(ally_profile), last_game_form(enemy_profile),
            ))

    recent_rate_contribution = covered_contribution(
        [ally - enemy for ally, enemy in recent_rate_pairs], 0.22, 3.0,
    )
    recent_kda_contribution = covered_contribution(
        [ally - enemy for ally, enemy in recent_kda_pairs], 0.18, 0.9,
    )
    streak_contribution = covered_contribution(
        [ally - enemy for ally, enemy in streak_pairs], 0.08, 0.5,
    )
    last_form_contribution = covered_contribution(
        [ally - enemy for ally, enemy in last_form_pairs], 0.30, 0.6,
    )

    # KDA, streak and previous-game result all overlap with the recent win-rate
    # sample.  Bound their combined incremental effect, then bound the complete
    # recent-form family so correlated evidence cannot dominate the forecast.
    extra_total = (
        recent_kda_contribution + streak_contribution + last_form_contribution
    )
    extra_scale = min(1.0, 1.2 / abs(extra_total)) if extra_total else 1.0
    recent_kda_contribution *= extra_scale
    streak_contribution *= extra_scale
    last_form_contribution *= extra_scale
    family_total = (
        recent_rate_contribution + recent_kda_contribution
        + streak_contribution + last_form_contribution
    )
    family_scale = min(1.0, 3.5 / abs(family_total)) if family_total else 1.0
    recent_rate_contribution *= family_scale
    recent_kda_contribution *= family_scale
    streak_contribution *= family_scale
    last_form_contribution *= family_scale

    def pair_averages(values: list[tuple[float, float]]) -> tuple[float, float]:
        return (
            sum(ally for ally, _enemy in values) / len(values),
            sum(enemy for _ally, enemy in values) / len(values),
        )

    if recent_rate_pairs:
        ally_average, enemy_average = pair_averages(recent_rate_pairs)
        factor_rows.append((
            "최근 폼", recent_rate_contribution,
            (
                f"최근 폼 아군 {ally_average:.1f}% · 적군 {enemy_average:.1f}%"
                f" · 같은 포지션 {len(recent_rate_pairs)}쌍"
            ),
        ))
    if recent_kda_pairs and abs(recent_kda_contribution) >= 0.01:
        ally_average, enemy_average = pair_averages(recent_kda_pairs)
        factor_rows.append((
            "최근 KDA", recent_kda_contribution,
            (
                f"최근 KDA 아군 {ally_average:.2f} · 적군 {enemy_average:.2f}"
                f" · {len(recent_kda_pairs)}쌍"
            ),
        ))
    if last_form_pairs and abs(last_form_contribution) >= 0.01:
        ally_average, enemy_average = pair_averages(last_form_pairs)
        factor_rows.append((
            "직전판 컨디션", last_form_contribution,
            (
                f"직전판 컨디션 아군 {ally_average:+.2f} · "
                f"적군 {enemy_average:+.2f} · {len(last_form_pairs)}쌍"
            ),
        ))
    if streak_pairs and abs(streak_contribution) >= 0.01:
        ally_average, enemy_average = pair_averages(streak_pairs)
        factor_rows.append((
            "연승·연패", streak_contribution,
            (
                f"연속 흐름 아군 {ally_average:+.1f} · "
                f"적군 {enemy_average:+.1f} · {len(streak_pairs)}쌍"
            ),
        ))

    recent_counts = (len(recent_rate_pairs), len(recent_rate_pairs))
    ally_recent_kda = [ally for ally, _enemy in recent_kda_pairs]
    enemy_recent_kda = [enemy for _ally, enemy in recent_kda_pairs]
    ally_last_form = [ally for ally, _enemy in last_form_pairs]
    enemy_last_form = [enemy for _ally, enemy in last_form_pairs]

    tier_order = {
        "IRON": 0, "BRONZE": 1, "SILVER": 2, "GOLD": 3,
        "PLATINUM": 4, "EMERALD": 5, "DIAMOND": 6,
        "MASTER": 7, "GRANDMASTER": 8, "CHALLENGER": 9,
    }
    division_order = {"IV": 0, "III": 1, "II": 2, "I": 3}

    def rank_values(players: list[LivePlayer]) -> list[float]:
        values: list[float] = []
        for player in players:
            profile = profiles.get(player.riot_id)
            if not profile or profile.tier.upper() not in tier_order:
                continue
            values.append(
                tier_order[profile.tier.upper()] * 4
                + division_order.get(profile.rank.upper(), 0)
                + clamp(profile.league_points / 100.0, 0.0, 1.0)
            )
        return values

    ally_ranks, enemy_ranks = rank_values(snapshot.allies), rank_values(snapshot.enemies)
    if ally_ranks and enemy_ranks:
        rank_difference = (
            sum(ally_ranks) / len(ally_ranks)
            - sum(enemy_ranks) / len(enemy_ranks)
        )
        contribution = clamp(rank_difference * 0.35, -3.0, 3.0)
        factor_rows.append((
            "티어", contribution,
            f"평균 티어 점수 차이 {rank_difference:+.1f}",
        ))

    ready_matchups = [
        stat for stat in lane_matchups.values()
        if stat.ally_win_rate is not None
    ]
    if ready_matchups:
        matchup_average = sum(
            stat.ally_win_rate or 50.0 for stat in ready_matchups
        ) / len(ready_matchups)
        contribution = clamp((matchup_average - 50.0) * 0.70, -4.5, 4.5)
        factor_rows.append((
            "라인 상성", contribution,
            f"OP.GG {len(ready_matchups)}라인 평균 {matchup_average:.1f}%",
        ))

    duo_priority = {"가능": 0.4, "유력": 0.8, "매우 유력": 1.2}
    pair_levels: dict[tuple[str, str], float] = {}
    for riot_id, values in duo_pairs.items():
        for other_id, level, _evidence in values:
            pair = tuple(sorted((riot_id.casefold(), other_id.casefold())))
            pair_levels[pair] = max(
                pair_levels.get(pair, 0.0), duo_priority.get(level, 0.0)
            )
    ally_ids = {player.riot_id.casefold() for player in snapshot.allies}
    enemy_ids = {player.riot_id.casefold() for player in snapshot.enemies}
    ally_duo = sum(
        value for pair, value in pair_levels.items() if set(pair) <= ally_ids
    )
    enemy_duo = sum(
        value for pair, value in pair_levels.items() if set(pair) <= enemy_ids
    )
    if ally_duo or enemy_duo:
        contribution = clamp(ally_duo - enemy_duo, -2.0, 2.0)
        factor_rows.append((
            "듀오", contribution,
            f"듀오 신호 아군 {ally_duo:.1f} · 적군 {enemy_duo:.1f}",
        ))

    probability = round(
        clamp(50.0 + sum(row[1] for row in factor_rows), 32.0, 68.0), 1
    )
    season_coverage = min(season_counts) / 5.0
    recent_coverage = min(recent_counts) / 5.0
    champion_coverage = min(champion_counts) / 5.0
    rank_coverage = min(len(ally_ranks), len(enemy_ranks)) / 5.0
    matchup_coverage = len(ready_matchups) / 5.0
    recent_kda_coverage = min(
        len(ally_recent_kda), len(enemy_recent_kda)
    ) / 5.0
    last_game_coverage = min(
        len(ally_last_form), len(enemy_last_form)
    ) / 5.0
    evidence_score = clamp(
        season_coverage * 0.30 + recent_coverage * 0.13
        + champion_coverage * 0.18 + rank_coverage * 0.05
        + matchup_coverage * 0.22 + recent_kda_coverage * 0.05
        + last_game_coverage * 0.07,
        0.0, 1.0,
    )
    confidence = (
        "높음" if evidence_score >= 0.75
        else "보통" if evidence_score >= 0.45 else "낮음"
    )
    evidence = tuple(
        row[2] for row in sorted(factor_rows, key=lambda row: abs(row[1]), reverse=True)
    ) or ("표본 수집 중 · 50% 기준값",)
    timestamp = (captured_at or datetime.now()).isoformat(timespec="seconds")
    return GamePrediction(
        prediction_key=live_game_prediction_key(snapshot),
        captured_at=timestamp,
        active_riot_id=active.riot_id if active else snapshot.active_riot_id,
        active_champion_id=active.champion_id if active else "",
        ally_champion_ids=tuple(player.champion_id for player in snapshot.allies),
        enemy_champion_ids=tuple(player.champion_id for player in snapshot.enemies),
        ally_riot_ids=tuple(player.riot_id for player in snapshot.allies),
        enemy_riot_ids=tuple(player.riot_id for player in snapshot.enemies),
        win_probability=probability,
        predicted_win=probability >= 50.0,
        confidence=confidence,
        evidence=evidence,
        evidence_score=round(evidence_score, 3),
    )


def live_champion_sample_from_payload(
    payload: dict,
    champion_id: str,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> tuple[int, int, int, int] | None:
    if str(payload.get("live_champion_id") or "") != champion_id:
        return None
    checked_at = str(payload.get("live_champion_checked_at") or "")
    try:
        checked = datetime.fromisoformat(checked_at)
    except (TypeError, ValueError):
        return None
    current = now or datetime.now(checked.tzinfo)
    if current.tzinfo is None and checked.tzinfo is not None:
        current = current.replace(tzinfo=checked.tzinfo)
    elif current.tzinfo is not None and checked.tzinfo is None:
        checked = checked.replace(tzinfo=current.tzinfo)
    if current - checked > max_age or current < checked:
        return None
    inspected = max(0, int(payload.get("live_champion_sample_games") or 0))
    games = max(0, int(payload.get("live_champion_games") or 0))
    wins = max(0, int(payload.get("live_champion_wins") or 0))
    target = max(inspected, int(payload.get("live_champion_sample_target") or 0))
    if inspected <= 0 or games > inspected or wins > games:
        return None
    return inspected, games, wins, target


def opgg_champion_stat_from_payload(
    payload: dict,
    champion_id: str,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> tuple[int, int, str] | None:
    if str(payload.get("opgg_champion_id") or "") != champion_id:
        return None
    checked_at = str(payload.get("opgg_champion_checked_at") or "")
    try:
        checked = datetime.fromisoformat(checked_at)
    except (TypeError, ValueError):
        return None
    current = now or datetime.now(checked.tzinfo)
    if current.tzinfo is None and checked.tzinfo is not None:
        current = current.replace(tzinfo=checked.tzinfo)
    elif current.tzinfo is not None and checked.tzinfo is None:
        checked = checked.replace(tzinfo=current.tzinfo)
    if current - checked > max_age or current < checked:
        return None
    wins = max(0, int(payload.get("opgg_champion_wins") or 0))
    losses = max(0, int(payload.get("opgg_champion_losses") or 0))
    page_updated = str(payload.get("opgg_page_updated_text") or "")
    return wins, losses, page_updated


def recent_match_ids_from_payload(
    payload: dict,
    now: datetime | None = None,
    max_age: timedelta | None = timedelta(hours=24),
) -> list[str] | None:
    """Return a cached Match-v5 ID page; ``None`` means a remote refresh is due."""
    raw_ids = payload.get("recent_match_ids")
    if not isinstance(raw_ids, list):
        return None
    match_ids = [str(match_id) for match_id in raw_ids if str(match_id)]
    if max_age is None:
        return match_ids
    checked_at = str(payload.get("recent_match_ids_checked_at") or "")
    try:
        checked = datetime.fromisoformat(checked_at)
    except (TypeError, ValueError):
        return None
    current = now or datetime.now(checked.tzinfo)
    if current.tzinfo is None and checked.tzinfo is not None:
        current = current.replace(tzinfo=checked.tzinfo)
    elif current.tzinfo is not None and checked.tzinfo is None:
        checked = checked.replace(tzinfo=current.tzinfo)
    if current < checked or current - checked > max_age:
        return None
    return match_ids


def matchup_build_reason(enemy_champion_id: str | None) -> tuple[str, str]:
    if not enemy_champion_id:
        return "상대 미확정", "상대가 정해지면 별도 대응 빌드를 구성"
    archetype = support_archetype(enemy_champion_id or "")
    return {
        "ENGAGE": ("이니시 대응", "폭딜·CC를 버티는 룬과 생존 아이템 우선"),
        "POKE": ("견제 대응", "지속 견제를 버티는 회복·유지력 선택지 우선"),
        "UTILITY": ("유틸 대응", "보호막·회복 싸움에 압박·치유 감소 선택지 우선"),
    }.get(archetype, ("균형 대응", "생존·유틸 아이템을 균형 있게 우선"))


def matchup_rune_index(
    rune_builds: list[RuneBuild], enemy_champion_id: str | None
) -> int:
    weights = MATCHUP_RUNE_WEIGHTS.get(
        support_archetype(enemy_champion_id or ""), {}
    )
    if not rune_builds or not weights:
        return 0
    scores = [
        sum(weights.get(int(perk.asset_id), 0) for perk in build.perks)
        for build in rune_builds
    ]
    return max(range(len(scores)), key=lambda index: (scores[index], -index))


def build_loadout_stat_text(
    games: int | None, win_rate: float | None,
) -> str:
    """Format only source-provided build statistics; never infer a sample."""
    sample = int(games or 0)
    parts: list[str] = []
    if win_rate is not None:
        parts.append(f"승률 {float(win_rate):.1f}%")
    if sample > 0:
        parts.append(f"{sample:,}게임")
    return " · ".join(parts) if parts else "표본 정보 없음"


def build_guide_has_statistics(guide: ChampionBuildGuide | None) -> bool:
    """Whether a guide uses the statistics-aware OP.GG cache schema."""
    if not guide:
        return False
    rune_stats = any(
        getattr(build, "games", None) is not None
        or getattr(build, "win_rate", None) is not None
        for build in guide.rune_builds
    )
    spell_stats = any(
        getattr(build, "games", None) is not None
        or getattr(build, "win_rate", None) is not None
        for build in getattr(guide, "summoner_spell_builds", [])
    )
    return rune_stats and spell_stats


def matchup_item_groups(
    groups: list[BuildItemGroup], enemy_champion_id: str | None
) -> list[BuildItemGroup]:
    priority = MATCHUP_ITEM_PRIORITY.get(
        support_archetype(enemy_champion_id or ""), ()
    )
    if not priority:
        return groups
    rank = {item_id: index for index, item_id in enumerate(priority)}
    adjusted: list[BuildItemGroup] = []
    for group in groups:
        if group.title in {"시작 아이템", "신발", "서포터 퀘스트 완성"}:
            adjusted.append(group)
            continue
        indexed = list(enumerate(group.items))
        items = [
            item for _index, item in sorted(
                indexed,
                key=lambda pair: (rank.get(int(pair[1].asset_id), 999), pair[0]),
            )
        ]
        adjusted.append(BuildItemGroup(group.title, items))
    return adjusted


def representative_build_item(
    groups: list[BuildItemGroup], variant: int = 0
) -> BuildAsset | None:
    """Pick one recognizable completed item for a compact build preset row."""
    preferred_titles = (
        "핵심 아이템", "완성 빌드", "4번째 아이템", "상황별 아이템",
        "5번째 아이템", "6번째 아이템",
    )
    for title in preferred_titles:
        items = next((group.items for group in groups if group.title == title), [])
        if items:
            return items[variant % len(items)]
    excluded = {"시작 아이템", "신발", "서포터 퀘스트 완성"}
    fallback = [
        item for group in groups if group.title not in excluded
        for item in group.items
    ]
    return fallback[variant % len(fallback)] if fallback else None


def final_item_builds(
    groups: list[BuildItemGroup], position: str = "SUPPORT", limit: int = 3
) -> list[BuildItemGroup]:
    """Compose distinct six-slot views from OP.GG's ranked item alternatives."""
    by_title = {group.title: list(group.items) for group in groups}
    support_items = by_title.get("서포터 퀘스트 완성", [])
    boots = by_title.get("신발", [])
    core = by_title.get("핵심 아이템", []) or by_title.get("완성 빌드", [])
    late_titles = (
        "4번째 아이템", "5번째 아이템", "6번째 아이템", "상황별 아이템"
    )
    late_groups = [by_title.get(title, []) for title in late_titles]
    excluded = {"시작 아이템", "신발", "서포터 퀘스트 완성"}
    fallback = [
        item for group in groups if group.title not in excluded
        for item in group.items
    ]
    results: list[BuildItemGroup] = []
    signatures: set[tuple[int, ...]] = set()

    def rotated(items: list[BuildAsset], offset: int) -> list[BuildAsset]:
        if not items:
            return []
        shift = offset % len(items)
        return items[shift:] + items[:shift]

    for variant in range(max(limit * 2, 3)):
        selected: list[BuildAsset] = []
        selected_ids: set[int] = set()

        def add(items: list[BuildAsset], count: int = 1) -> None:
            for item in rotated(items, variant):
                item_id = int(item.asset_id)
                if item_id in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(item_id)
                if len(selected) >= 6 or count <= 1:
                    break
                count -= 1

        if str(position).upper() in {"SUPPORT", "UTILITY"}:
            add(support_items)
        add(boots)
        add(core, 3)
        for items in late_groups:
            if len(selected) >= 6:
                break
            add(items)
        if len(selected) < 6:
            add(fallback, 6 - len(selected))
        if len(selected) != 6:
            continue
        signature = tuple(int(item.asset_id) for item in selected)
        if signature in signatures:
            continue
        signatures.add(signature)
        results.append(BuildItemGroup(f"추천 완성 빌드 {len(results) + 1}", selected))
        if len(results) >= limit:
            break
    return results


def matchup_final_item_builds(
    groups: list[BuildItemGroup], enemy_champion_id: str | None,
    position: str = "SUPPORT", limit: int = 2,
) -> list[BuildItemGroup]:
    """Build a separate six-slot pool with matchup items promoted globally."""
    if not enemy_champion_id:
        return []
    priority = MATCHUP_ITEM_PRIORITY.get(
        support_archetype(enemy_champion_id or ""), ()
    )
    if not priority:
        return []
    rank = {item_id: index for index, item_id in enumerate(priority)}
    excluded = {"시작 아이템", "신발", "서포터 퀘스트 완성"}
    pool: list[BuildAsset] = []
    seen: set[int] = set()
    for group in groups:
        if group.title in excluded:
            continue
        for item in group.items:
            item_id = int(item.asset_id)
            if item_id not in seen:
                seen.add(item_id)
                pool.append(item)
    indexed = list(enumerate(pool))
    prioritized = [
        item for _index, item in sorted(
            indexed,
            key=lambda pair: (rank.get(int(pair[1].asset_id), 999), pair[0]),
        )
    ]
    synthetic = [
        group for group in groups
        if group.title in {"신발", "서포터 퀘스트 완성"}
    ]
    synthetic.append(BuildItemGroup("핵심 아이템", prioritized))
    return final_item_builds(synthetic, position, limit)


def team_objective_counts(team_payload: dict) -> dict[str, int]:
    objectives = team_payload.get("objectives") or {}
    return {
        key: int((objectives.get(source) or {}).get("kills") or 0)
        for key, source in (
            ("void_grubs", "horde"),
            ("rift_heralds", "riftHerald"),
            ("dragons", "dragon"),
            ("barons", "baron"),
            ("towers", "tower"),
        )
    }


class _HoverTooltip:
    """Small delayed tooltip that stays inside the owning Tk application."""

    def __init__(self, widget: tk.Widget, text_provider: Callable[[], str]) -> None:
        self.widget = widget
        self.text_provider = text_provider
        self.window: tk.Toplevel | None = None
        self.after_id: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        self.after_id = self.widget.after(280, self._show)

    def _cancel(self) -> None:
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    def _show(self) -> None:
        self.after_id = None
        try:
            if not self.widget.winfo_exists():
                return
            text = self.text_provider().strip()
            if not text:
                return
            window = tk.Toplevel(self.widget)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.configure(bg=COLORS["gold"], padx=1, pady=1)
            tk.Label(
                window, text=text, justify="left", anchor="w", wraplength=390,
                bg="#0b1220", fg=COLORS["text"], padx=11, pady=9,
                font=("Malgun Gothic", 8),
            ).pack()
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
            window.geometry(f"+{x}+{y}")
            self.window = window
        except tk.TclError:
            self.window = None

    def _hide(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        if self.window:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None


def candidate_score(
    counter: OpggCounter,
    personal: PersonalStat | None = None,
    synergy: OpggSynergyStat | None = None,
) -> tuple[float, str]:
    """Blend matchup, local experience, and bot-lane pairing without hiding risk."""
    score = 50.0 + (counter.versus_win_rate - 50.0) * 2.8
    confidence_points = 2 if counter.games >= 5000 else (1 if counter.games >= 1500 else 0)
    if personal:
        if personal.games >= 3 and personal.win_rate is not None:
            weight = min(personal.games / 20.0, 1.0)
            score += (personal.win_rate - 50.0) * 0.22 * weight
        if personal.matchup_games >= 2 and personal.matchup_win_rate is not None:
            weight = min(personal.matchup_games / 10.0, 1.0)
            score += (personal.matchup_win_rate - 50.0) * 0.35 * weight
        confidence_points += 2 if personal.games >= 15 else (1 if personal.games >= 5 else 0)
        confidence_points += (
            2 if personal.matchup_games >= 8 else (1 if personal.matchup_games >= 3 else 0)
        )
        if personal.ally_adc_games >= 2 and personal.ally_adc_win_rate is not None:
            weight = min(personal.ally_adc_games / 12.0, 1.0)
            score += (personal.ally_adc_win_rate - 50.0) * 0.55 * weight
            confidence_points += (
                2 if personal.ally_adc_games >= 10
                else 1 if personal.ally_adc_games >= 4 else 0
            )
    if synergy and synergy.win_rate is not None and synergy.games >= 30:
        weight = min(synergy.games / 1500.0, 1.0)
        score += (synergy.win_rate - 50.0) * 1.35 * weight
        confidence_points += 2 if synergy.games >= 1000 else (
            1 if synergy.games >= 250 else 0
        )
    confidence = "높음" if confidence_points >= 5 else ("보통" if confidence_points >= 3 else "낮음")
    return max(0.0, min(100.0, score)), confidence


def local_recommendations_from_candidates(
    counters: list[OpggCounter],
    *,
    unavailable: set[str] | None = None,
    personal_stats: dict[str, PersonalStat | None] | None = None,
    synergies: dict[str, OpggSynergyStat | None] | None = None,
    enemy_name: str = "",
    ally_adc_name: str = "",
    role_name: str = "서포터",
    language: str = "ko",
) -> list[Recommendation]:
    """Build an immediate local top three without waiting for Codex.

    The rows are deliberately derived only from data already held in memory.
    This keeps first-pick/blind recommendations instant and never starts an
    external request from the renderer.
    """
    blocked = unavailable or set()
    stats = personal_stats or {}
    synergy_by_id = synergies or {}
    unique: dict[str, OpggCounter] = {}
    for counter in counters:
        if counter.champion_id and counter.champion_id not in blocked:
            unique.setdefault(counter.champion_id, counter)
    ranked = sorted(
        unique.values(),
        key=lambda counter: candidate_score(
            counter, stats.get(counter.champion_id),
            synergy_by_id.get(counter.champion_id),
        )[0],
        reverse=True,
    )[:3]
    english = normalize_language(language) == "en"
    result: list[Recommendation] = []
    for rank, counter in enumerate(ranked, start=1):
        synergy = synergy_by_id.get(counter.champion_id)
        if counter.games >= 1_500 and counter.versus_win_rate >= 52.0:
            safety = "High" if english else "높음"
        elif counter.games >= 300 and counter.versus_win_rate >= 49.5:
            safety = "Medium" if english else "보통"
        else:
            safety = "Low" if english else "낮음"
        if english:
            style = "Matchup-first" if enemy_name else "Blind first-pick"
            reason = (
                f"Local OP.GG data: {counter.versus_win_rate:.1f}% into {enemy_name}."
                if enemy_name else
                f"Immediate {role_name} meta candidate at {counter.versus_win_rate:.1f}%."
            )
            team = (
                f"{ally_adc_name} pairing: {synergy.win_rate:.1f}% over {synergy.games:,} games."
                if ally_adc_name and synergy and synergy.win_rate is not None else
                "Flexible local-data candidate while the ally composition is incomplete."
            )
            lane = (
                "Use the favorable matchup window, but confirm summoner spells before committing."
                if enemy_name and counter.versus_win_rate >= 50.0 else
                "Keep the lane plan flexible until the opposing lane is revealed."
            )
            watch = (
                "Small sample: treat this as a quick reference."
                if counter.games < 300 else
                "Recheck bans, ally picks, and the final enemy lane before lock-in."
            )
        else:
            style = "상성 우선" if enemy_name else "블라인드 선픽"
            reason = (
                f"로컬 OP.GG 기준 {enemy_name} 상대 승률 {counter.versus_win_rate:.1f}%."
                if enemy_name else
                f"적 {role_name} 미확정 상태의 즉시 메타 후보 · 승률 {counter.versus_win_rate:.1f}%."
            )
            team = (
                f"{ally_adc_name} 조합 승률 {synergy.win_rate:.1f}% · {synergy.games:,}게임."
                if ally_adc_name and synergy and synergy.win_rate is not None else
                "아군 조합이 덜 공개된 상태에서도 쓰기 쉬운 로컬 데이터 후보."
            )
            lane = (
                "상성 우위를 활용하되 상대 스펠과 정글 위치를 확인한 뒤 교전."
                if enemy_name and counter.versus_win_rate >= 50.0 else
                "상대 라인이 공개되기 전에는 무리한 선공보다 대응 여지를 남김."
            )
            watch = (
                "표본이 작으므로 빠른 참고용으로만 사용."
                if counter.games < 300 else
                "확정 전 밴·아군 픽·최종 상대 라인을 다시 확인."
            )
        result.append(Recommendation(
            rank=rank,
            champion_id=counter.champion_id,
            champion_name_ko=counter.champion_name_ko,
            style=style,
            blind_safety=safety,
            reason=reason,
            team_synergy=team,
            lane_plan=lane,
            watch_for=watch,
        ))
    return result


def merge_codex_with_local_recommendations(
    codex_recommendations: list[Recommendation],
    local_recommendations: list[Recommendation],
    *,
    unavailable: set[str] | None = None,
    limit: int = 3,
) -> list[Recommendation]:
    """Keep valid Codex picks and fill only missing slots from local data."""
    blocked = unavailable or set()
    merged: list[Recommendation] = []
    seen: set[str] = set()
    for item in [*codex_recommendations, *local_recommendations]:
        if item.champion_id in blocked or item.champion_id in seen:
            continue
        seen.add(item.champion_id)
        merged.append(replace(item, rank=len(merged) + 1))
        if len(merged) >= max(0, int(limit)):
            break
    return merged


class AdvisorApp:
    def __init__(
        self,
        root: tk.Tk,
        storage: Storage,
        registry: ChampionRegistry,
        demo: bool = False,
        asset_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.storage = storage
        self.registry = registry
        self.demo = demo
        self.asset_dir = asset_dir or storage.db_path.parent
        self.ui_language = normalize_language(
            storage.get_setting("ui_language", "ko")
        )
        self._champion_name_translations = {
            name_ko: champion_id
            for champion_id, (_champion_key, name_ko) in registry.by_id.items()
        }
        # Translation bookkeeping must never own Tk widgets.  A normal set
        # kept every destroyed card alive after an English render, which was
        # especially visible while live-player cards were refreshed.
        self._language_mapped_widgets: weakref.WeakSet[tk.Misc] = weakref.WeakSet()
        self._language_tracked_widgets: weakref.WeakSet[tk.Misc] = weakref.WeakSet()
        self._language_map_after_id: str | None = None
        self._language_refresh_after_id: str | None = None
        self._closing = False
        self._shutdown_event = threading.Event()
        # Reusing a bounded worker pool prevents a burst of profile/icon/cache
        # work from creating an unbounded number of native threads.  Most call
        # sites already have single-flight guards; this is the final backstop.
        self._background_executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="advisor-bg",
        )
        if demo:
            # These values live in the temporary demo database created by
            # main.py.  They are deliberately generic and can never leak the
            # user's real Riot identity or settings into public screenshots.
            storage.set_setting("riot_game_name", "DemoPlayer")
            storage.set_setting("riot_tag_line", "DEMO")
            storage.set_setting("riot_puuid", "demo-player-puuid")
            # Keep the public preview complete while external Codex/LCU
            # actions remain blocked in demo mode.
            storage.set_setting("codex_recommendations_enabled", "1")
            storage.save_live_profile(
                "DemoPlayer#DEMO",
                "demo-player-puuid",
                {
                    "solo_entry": {
                        "tier": "EMERALD", "rank": "II", "leaguePoints": 64,
                        "wins": 42, "losses": 36,
                    }
                },
            )
        self._data_preferences: dict[str, int] = {}
        self._reload_data_preferences()
        self.lcu = LcuClient(storage.get_setting("lcu_lockfile_path"))
        self.live_client = LiveClient(registry)
        try:
            stored_live_identities = json.loads(
                storage.get_setting(LIVE_IDENTITY_CACHE_SETTING, "") or "null"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            stored_live_identities = None
        self._live_identity_lock = threading.RLock()
        self._live_identity_payload: dict[str, object] | None = (
            stored_live_identities if isinstance(stored_live_identities, dict) else None
        )
        self._live_identity_generation = 0
        self._live_identity_capture_lock = threading.Lock()
        self._live_identity_capture_running = False
        self._live_identity_resolution_auth_failed = False
        self._live_identity_audit_path = (
            storage.db_path.parent / "live_identity_audit.jsonl"
        )
        self._live_identity_audit_lock = threading.Lock()
        self.opgg_client = OpggClient(registry)
        self.build_applicator = BuildApplicator(self.lcu, registry)
        self.icon_cache = ChampionIconCache(root, registry, self.asset_dir / "icons")
        self.item_icon_cache = ItemIconCache(root, registry, self.asset_dir / "items")
        self.build_icon_cache = RemoteIconCache(
            root, self.asset_dir / "build_assets"
        )
        self.build_asset_preloader = BuildAssetPreloader(
            registry,
            self.asset_dir / "items",
            self.asset_dir / "build_assets",
        )
        self.rune_catalog = RuneCatalog(
            self.asset_dir / "runes" / "runes-ko_KR.json"
        )
        self.draft = self._demo_draft() if demo else DraftSnapshot()
        # The live endpoint is polled every three seconds.  Keep the last full
        # draft context in memory so attaching pick-order information never
        # opens SQLite on every steady in-game poll.
        self._draft_pick_context_signature = ""
        self._cached_draft_pick_context: dict[str, dict[str, list[int]]] | None = None
        self._draft_pick_context_cache_loaded = False
        if demo:
            self.opgg_meta_snapshot = self._demo_opgg_meta()
            self.opgg_snapshot = self._demo_opgg()
            self.opgg_synergy_snapshot = self._demo_opgg_synergy()
        else:
            self.opgg_meta_snapshot = storage.load_opgg_snapshot(None, self.draft.my_role)
            self.opgg_snapshot = self.opgg_meta_snapshot
            self.opgg_synergy_snapshot: OpggSynergySnapshot | None = None
        self.live_game = self._demo_live_game() if demo else LiveGameSnapshot()
        if demo:
            self._attach_draft_pick_context(self.live_game)
        self.player_profiles = self._demo_player_profiles() if demo else {}
        self.opgg_player_profiles: dict[str, OpggMcpSummonerProfile] = {}
        self.duo_pairs: dict[str, list[tuple[str, str, str]]] = (
            self._demo_duo_pairs() if demo else {}
        )
        self.lane_matchups: dict[str, LaneMatchupStat] = (
            self._demo_lane_matchups() if demo else {}
        )
        self._live_prediction: GamePrediction | None = None
        # Keep the last completed live board in memory. It is intentionally
        # session-only: reopening the app starts with a clean play tab, while a
        # finished game can still be reviewed without new Riot/OP.GG requests.
        self._previous_play_state: dict[str, object] | None = None
        self._showing_previous_play = False
        self._prediction_saved_signature = ""
        self._prediction_save_pending_signature = ""
        self._prediction_save_after_id: str | None = None
        self._prediction_save_running = False
        self._prediction_save_queued: tuple[GamePrediction, str] | None = None
        self._prediction_baseline_key = ""
        self._prediction_baseline: GamePrediction | None = None
        self._prediction_settle_started_at = 0.0
        self._prediction_settle_after_id: str | None = None
        self._play_prediction_signature: tuple[object, ...] | None = ()
        self._play_summary_signature = ""
        self.jungle_tendencies: dict[str, JungleTendencyStat] = (
            self._demo_jungle_tendencies() if demo else {}
        )
        self.recommendations: list[Recommendation] = []
        self.recommendation_source = ""
        self.recommendation_snapshot_id = ""
        self.recommendation_context_signature = ""
        self.recommendation_enemy_support_id = ""
        self._local_recommendation_signature = ""
        self._local_fallback_candidates_by_role: dict[str, list[OpggCounter]] = {}
        self._recommendation_generation = 0
        self._champ_select_inner_phase = ""
        self._local_pick_action_in_progress = False
        self._recommendation_action_buttons: list[tuple[tk.Button, str]] = []
        self._codex_cli_running = False
        self._codex_cli_error = ""
        self._recommendation_apply_error = ""
        try:
            self.codex_cli: CodexCliClient | None = CodexCliClient(
                storage.db_path.parent / "codex_dialog"
            )
        except CodexCliError as exc:
            self.codex_cli = None
            self._codex_cli_error = str(exc)
        self.auto_accept_enabled = storage.get_setting("auto_accept_enabled", "0") == "1"
        self._auto_accept_lock = threading.RLock()
        self._auto_accept_generation = 0
        self._auto_accept_monitoring = False
        self._auto_accept_cycle_seen = False
        self._auto_accept_cancel = threading.Event()
        self._auto_accept_deadline = 0.0
        # ``lux_auto_ban_enabled`` was the original public setting.  Read it
        # only as a migration fallback so existing users keep their choice,
        # while new versions expose a champion-agnostic auto-ban feature.
        stored_auto_ban = storage.get_setting("auto_ban_enabled")
        if not stored_auto_ban:
            stored_auto_ban = storage.get_setting("lux_auto_ban_enabled", "0")
            storage.set_setting("auto_ban_enabled", stored_auto_ban)
        self.lux_auto_ban_enabled = stored_auto_ban == "1"
        try:
            selected_auto_ban_key = int(
                storage.get_setting("auto_ban_champion_key", "99") or 99
            )
        except ValueError:
            selected_auto_ban_key = 99
        self.auto_ban_champion_key = (
            selected_auto_ban_key
            if selected_auto_ban_key in registry.by_key else 99
        )
        self.codex_recommendations_enabled = (
            storage.get_setting("codex_recommendations_enabled", "0") == "1"
        )
        self.stop_queue_after_dodge_enabled = (
            storage.get_setting("stop_queue_after_dodge_enabled", "0") == "1"
        )
        self._auto_accept_status = "게임 수락 대기"
        self._lux_auto_ban_status = "내 밴 차례 대기"
        self._lux_auto_ban_action_id: int | None = None
        self._lux_auto_ban_target_remaining_ms = 0
        self._lux_auto_ban_fallback_deadline = 0.0
        # Auto-ban timing must keep running even when Tk is busy rendering a
        # large draft.  This lock protects the small controller state shared by
        # the LCU poller, the dedicated monitor, and UI toggle callbacks.
        self._lux_auto_ban_lock = threading.RLock()
        self._lux_auto_ban_generation = 0
        self._lux_auto_ban_monitoring = False
        self._lux_auto_ban_completed_action_id: int | None = None
        self._lux_auto_ban_retry_after = 0.0
        self._lux_auto_ban_last_remaining_ms: int | None = None
        self._lux_auto_ban_last_sampled_at = 0.0
        self._lux_auto_ban_staged = False
        self._lux_auto_ban_display_signature: tuple[object, ...] | None = None
        self._lux_auto_ban_watcher_running = False
        self._lux_auto_ban_watcher_wake = threading.Event()
        self._lux_auto_ban_audit_path = (
            storage.db_path.parent / "auto_ban_audit.jsonl"
        )
        self._lux_auto_ban_audit_lock = threading.Lock()
        self._pick_order_change_notice = ""
        self._pick_order_notice_after_id: str | None = None
        self._champion_action_running = False
        self._lcu_polling = False
        self._opgg_refreshing = False
        self._selection_matchup_refreshing: set[str] = set()
        self._synergy_refreshing = False
        self._synergy_checked_adc = ""
        self._riot_syncing = False
        self._post_game_sync_generation = 0
        self._post_game_sync_after_id: str | None = None
        self._post_game_sync_baseline_match_id = ""
        self._live_polling = False
        self._profiles_loading = False
        self._profile_reload_requested = False
        self._opgg_profiles_loading = False
        self._opgg_profile_reload_requested = False
        self._opgg_profiles_checked_signature = ""
        self._opgg_profile_failures = 0
        self._cache_manager_window: tk.Toplevel | None = None
        self._cache_manager_rows: dict[str, tuple[tk.Label, tk.Button]] = {}
        self._cache_manager_message: tk.Label | None = None
        self._cache_manager_running = ""
        self._cache_manager_notebook: ttk.Notebook | None = None
        self._cache_manager_overview_content: tk.Frame | None = None
        self._cache_manager_overview_canvas: tk.Canvas | None = None
        self._cache_manager_champion_content: tk.Frame | None = None
        self._cache_manager_champion_canvas: tk.Canvas | None = None
        self._cache_manager_position_notebook: ttk.Notebook | None = None
        self._cache_manager_position_tabs: dict[str, tk.Frame] = {}
        self._cache_manager_champion_contents: dict[str, tk.Frame] = {}
        self._cache_manager_champion_canvases: dict[str, tk.Canvas] = {}
        self._cache_manager_champion_widgets: dict[
            tuple[str, str], dict[str, object]
        ] = {}
        self._cache_manager_rendered_queries: dict[str, str] = {}
        initial_cache_position = str(self.draft.my_role or "SUPPORT").upper()
        self._cache_manager_active_position = (
            "SUPPORT" if initial_cache_position == "UTILITY"
            else initial_cache_position
        )
        self._cache_manager_search_var: tk.StringVar | None = None
        self._cache_manager_count_label: tk.Label | None = None
        self._cache_manager_render_after_id: str | None = None
        self._duo_checking = False
        self._duo_checked_signature = ""
        self._lane_matchup_refreshing: set[str] = set()
        self._manual_enemy_support: str | None = None
        self._support_catalog_ids: set[str] | None = None
        self._live_signature = ""
        self._live_active_signature = ""
        # The live endpoint is polled every few seconds, but the ten player cards
        # should only be rebuilt when card data actually changes. Recreating every
        # Tk widget on each poll caused the whole play board to visibly flash.
        self._play_roster_signature = ""
        self._play_card_signatures: dict[str, str] = {}
        self._play_insight_signature = ""
        self._play_insight_section_signatures: dict[str, str] = {}
        self._play_insight_after_id: str | None = None
        self._jungle_tendency_context: tuple[object, ...] | None = None
        self._jungle_tendency_loading = False
        self.player_behaviors: dict[str, PlayerBehaviorStat] = {}
        self._lane_opponent_analysis_context: tuple[object, ...] | None = None
        self._lane_opponent_personal_stat: PersonalStat | None = None
        self._lane_opponent_behavior: PlayerBehaviorStat | None = (
            self._demo_player_behavior() if demo else None
        )
        self._lane_opponent_analysis_loading = False
        self._my_account_analysis_context: tuple[object, ...] | None = None
        self._my_personal_stat: PersonalStat | None = (
            self._demo_my_personal_stat() if demo else None
        )
        self._my_behavior: PlayerBehaviorStat | None = (
            self._demo_my_player_behavior() if demo else None
        )
        self._my_account_analysis_loading = False
        self._selection_render_scheduled = False
        self._selection_render_after_id: str | None = None
        self._selection_panel_signatures: dict[str, str] = {}
        self._selection_panel_revisions: dict[str, int] = {}
        self._selection_asset_after_id: str | None = None
        self._selection_asset_pending_panels: set[str] = set()
        self._scrollregion_after_ids: dict[tk.Canvas, str] = {}
        self._scrollregion_bounds: dict[tk.Canvas, tuple[int, int, int, int]] = {}
        self._main_tab_activation_after_id: str | None = None
        self._active_main_tab_index = 0
        self._selection_detail_save_after_id: str | None = None
        self._pending_selection_detail_index = 0
        self._tab_build_refresh_attempted: set[tuple[str, str]] = set()
        self._hover_matchup_signature = ""
        self._hover_matchup_render_scheduled = False
        self._hover_personal_context: tuple[
            tuple[int, int], str, str, str, str | None, str | None,
        ] | None = None
        self._hover_personal_stat: PersonalStat | None = None
        self._hover_personal_loading: set[tuple[object, ...]] = set()
        self._hover_matchup_errors: dict[str, str] = {}
        self._play_render_scheduled = False
        self._play_render_after_id: str | None = None
        self._personal_cache_context: tuple[
            tuple[int, int], str, str, str | None, str | None,
        ] | None = None
        self._personal_stats_cache: dict[str, PersonalStat] = {}
        self._personal_stats_pending: set[str] = set()
        self._personal_stats_loading = False
        self._personal_load_scheduled = False
        self._support_filter = "ALL"
        self.history_overview: HistoryOverview | None = (
            self._demo_history_overview() if demo else None
        )
        self._history_loading = False
        self._history_reload_requested = False
        self._history_revision: tuple[int, int] | None = None
        self._history_visible_count = 10
        self._history_result_filter = "ALL"
        self._history_position_filter = "ALL"
        self._history_render_scheduled = False
        self._history_render_after_id: str | None = None
        self._history_asset_revision = 0
        self._history_content_signature = ""
        self._history_champion_signature = ""
        self._history_matches_signature = ""
        self._history_recent_champions_signature = ""
        # The main third page owns a fixed, non-closable history tab. Every
        # clicked player receives an isolated tab and an independent 10-game
        # Riot pagination cursor so it can never enter the owner's 1,000-game
        # synchronization path.
        self._player_history_tabs: dict[str, dict[str, object]] = {}
        self._player_history_tab_keys: dict[str, str] = {}
        self._player_history_generation = 0
        self._player_history_profile_inflight: set[str] = set()
        self._player_history_profile_semaphore = threading.Semaphore(3)
        # Owner 1,000-game synchronization and explicit 10-game player pages
        # share one request lane.  This avoids development-key bursts while
        # keeping all SQLite decoding and Tk work outside the lock.
        self._riot_history_request_lock = threading.Lock()
        self._non_owner_prune_running = False
        self._non_owner_prune_after_id: str | None = None
        self._build_selected_champion_id = storage.get_setting(
            "build_selected_champion", "Thresh"
        )
        if self._build_selected_champion_id not in registry.by_id:
            self._build_selected_champion_id = "Thresh"
        if demo:
            self._build_selected_champion_id = "Thresh"
        self._flash_slot = storage.get_setting("flash_slot", "F").upper()
        if self._flash_slot not in {"D", "F"}:
            self._flash_slot = "F"
        self.build_guide: ChampionBuildGuide | None = (
            self._demo_build() if demo else storage.load_build_guide(
                self._build_selected_champion_id, self.draft.my_role
            )
        )
        self._build_rune_index = 0
        self._build_spell_index = 0
        self._build_item_details_expanded = False
        self._prompt_copied_snapshot_id = ""
        self._build_rune_manual = False
        self._rune_editor_source = ""
        self._rune_editor_custom = False
        self._rune_primary_style_id = 0
        self._rune_sub_style_id = 0
        self._rune_primary_perks: list[int] = []
        self._rune_secondary_perks: dict[int, int] = {}
        self._rune_secondary_order: list[int] = []
        self._rune_shards: list[int] = []
        self._rune_choice_widgets: dict[
            tuple[str, int, int], tuple[tk.Button, str]
        ] = {}
        self._rune_editor_hint_label: tk.Label | None = None
        self._rune_editor_summary_label: tk.Label | None = None
        self._rune_apply_button: tk.Button | None = None
        self._rune_catalog_refreshing = False
        self._build_matchup_signature = ""
        self._build_refreshing = False
        self._build_applying = False
        self._build_bulk_downloading = False
        self._build_bulk_cancel = threading.Event()
        self._build_request_signature = ""
        self._build_render_signature = ""
        self._build_preset_widgets: list[
            tuple[tk.Widget, tk.Widget, tk.Widget, int]
        ] = []
        self._build_spell_choice_widgets: list[
            tuple[tk.Widget, tk.Widget, tk.Widget, int]
        ] = []
        self._build_item_details_frame: tk.Frame | None = None
        self._build_item_details_toggle: tk.Button | None = None
        self._build_item_apply_button: tk.Button | None = None
        self._build_spell_row: tk.Frame | None = None
        self._flash_slot_buttons: dict[str, tk.Button] = {}
        self.game_phase = "DEMO" if demo else "None"
        self._identity_checked = demo
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

        self._configure_root()
        self._configure_styles()
        self._build_ui()
        if self.build_guide:
            self._prefetch_build_assets(self.build_guide)
        self._render_all()
        self.root.after(80, self._drain_ui_queue)
        self.root.after(1000, self._tick)
        self.root.after(
            LUX_AUTO_BAN_DISPLAY_INTERVAL_MS,
            self._tick_lux_auto_ban_display,
        )
        self.root.after(100, self._refresh_registry_background)
        self.root.after(220, self._refresh_rune_catalog_background)
        self.root.after(
            180, lambda: self.icon_cache.prefetch_all(
                self._invalidate_all_champion_icon_panels
            )
        )
        self.root.after(420, self._ensure_history_loaded)
        self._schedule_non_owner_prune(900)
        if not self.demo:
            self._start_lux_auto_ban_watcher()
            self._audit_lux_auto_ban(
                "app_started", enabled=self.lux_auto_ban_enabled,
            )
            self.root.after(250, self._poll_lcu)

    def _configure_root(self) -> None:
        self.root.title(self._tr("LOL Support Advisor"))
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry("1460x920")
        self.root.minsize(1120, 760)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass
        self.root.option_add("*TCombobox*Listbox.background", COLORS["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["surface_selected"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["text"])
        self.root.bind_all("<Map>", self._on_widget_mapped, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        """Flush only a baseline that already passed the settle gate."""
        if getattr(self, "_closing", False):
            return
        self._closing = True
        queued = getattr(self, "_prediction_save_queued", None)
        prediction = queued[0] if queued else None
        if (
            not self.demo
            and isinstance(prediction, GamePrediction)
            and prediction.prediction_key
            and prediction.active_riot_id
            and prediction.evidence_score >= 0.15
            and len(getattr(self, "live_game", LiveGameSnapshot()).players) == 10
        ):
            try:
                self.storage.save_game_prediction(prediction)
            except Exception:
                # Closing the UI must remain possible even if SQLite is busy;
                # the background single-flight writer may still finish first.
                pass
        # Stop producers before destroying Tk. Otherwise persistent LCU
        # watchers and completed network workers can keep enqueueing closures
        # that retain the complete application/widget graph.
        self._shutdown_event.set()
        self._lux_auto_ban_watcher_wake.set()
        self._auto_accept_cancel.set()
        self._build_bulk_cancel.set()
        with self._lux_auto_ban_lock:
            self._lux_auto_ban_generation += 1
            self._lux_auto_ban_monitoring = False
        with self._auto_accept_lock:
            self._auto_accept_generation += 1
            self._auto_accept_monitoring = False

        # Cancel every remembered Tk timer, including section debounce timers.
        # root.destroy() also drops Tcl timers, but cancelling here releases
        # their Python closures immediately and makes repeated app construction
        # in tests/tools leak-free.
        for name, callback_id in tuple(vars(self).items()):
            if not name.endswith("_after_id") or not callback_id:
                continue
            try:
                self.root.after_cancel(callback_id)
            except (TypeError, tk.TclError):
                pass
            try:
                setattr(self, name, None)
            except AttributeError:
                pass
        for callback_id in tuple(self._scrollregion_after_ids.values()):
            try:
                self.root.after_cancel(callback_id)
            except tk.TclError:
                pass
        self._scrollregion_after_ids.clear()
        self._scrollregion_bounds.clear()
        self._language_mapped_widgets.clear()
        self._language_tracked_widgets.clear()
        try:
            self._background_executor.shutdown(wait=False, cancel_futures=True)
        except (AttributeError, RuntimeError):
            pass
        while True:
            try:
                self._ui_queue.get_nowait()
            except queue.Empty:
                break
        self.root.destroy()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Advisor.Treeview",
            background=COLORS["panel_2"],
            fieldbackground=COLORS["panel_2"],
            foreground=COLORS["text"],
            rowheight=34,
            borderwidth=0,
            font=("Malgun Gothic", 9),
        )
        style.configure(
            "Advisor.Treeview.Heading",
            background=COLORS["chip"],
            foreground=COLORS["muted"],
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
        )
        style.map("Advisor.Treeview", background=[("selected", "#29476f")])
        style.configure(
            "Advisor.TNotebook", background=COLORS["bg"], borderwidth=0, tabmargins=(22, 4, 0, 0)
        )
        style.configure(
            "Advisor.TNotebook.Tab", background=COLORS["panel"], foreground=COLORS["muted"],
            padding=(30, 11), font=("Malgun Gothic", 10, "bold"), borderwidth=0,
        )
        style.configure(
            "Selection.TNotebook",
            background=COLORS["panel"], borderwidth=0, tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "Selection.TNotebook.Tab",
            background=COLORS["surface"], foreground=COLORS["muted"],
            padding=(22, 9), font=("Malgun Gothic", 9, "bold"), borderwidth=0,
        )
        style.configure(
            "Advisor.TCombobox",
            fieldbackground=COLORS["surface"], background=COLORS["chip"],
            foreground=COLORS["text"], arrowcolor=COLORS["muted"],
            bordercolor=COLORS["border"], lightcolor=COLORS["border"],
            darkcolor=COLORS["border"], padding=5,
        )
        style.map(
            "Advisor.TCombobox",
            fieldbackground=[("readonly", COLORS["surface"])],
            foreground=[("readonly", COLORS["text"])],
            selectbackground=[("readonly", COLORS["surface_selected"])],
            selectforeground=[("readonly", COLORS["text"])],
        )
        style.configure(
            "Advisor.Vertical.TScrollbar",
            background=COLORS["chip"], troughcolor=COLORS["bg"],
            bordercolor=COLORS["bg"], arrowcolor=COLORS["muted"],
            darkcolor=COLORS["chip"], lightcolor=COLORS["chip"],
        )
        style.configure(
            "Build.Horizontal.TProgressbar",
            troughcolor=COLORS["panel_2"], background=COLORS["green"],
            darkcolor=COLORS["green"], lightcolor=COLORS["green"],
            bordercolor=COLORS["border"], thickness=7,
        )
        style.map(
            "Advisor.TNotebook.Tab",
            background=[("selected", COLORS["panel_2"])],
            foreground=[("selected", COLORS["gold"])],
        )
        style.map(
            "Selection.TNotebook.Tab",
            background=[("selected", COLORS["surface_selected"])],
            foreground=[("selected", COLORS["blue"])],
        )

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=COLORS["bg"])
        shell.pack(fill="both", expand=True)
        self._build_header(shell)
        self.notebook = ttk.Notebook(shell, style="Advisor.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.selection_tab, self.selection_canvas, self.selection_content = self._scroll_tab()
        self.play_tab, self.play_canvas, self.play_content = self._scroll_tab()
        self.history_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.history_notebook = ttk.Notebook(
            self.history_tab, style="Selection.TNotebook"
        )
        self.history_notebook.pack(fill="both", expand=True)
        (
            self.history_home_tab,
            self.history_canvas,
            self.history_content,
        ) = self._scroll_tab(self.history_notebook)
        self.history_home_canvas = self.history_canvas
        self.history_notebook.add(self.history_home_tab, text="내 전적")
        self.history_notebook.bind(
            "<<NotebookTabChanged>>", self._on_player_history_tab_changed,
        )
        self.build_tab, self.build_canvas, self.build_content = self._scroll_tab()
        self.notebook.add(self.selection_tab, text="1  선택창")
        self.notebook.add(self.play_tab, text="2  플레이")
        self.notebook.add(self.history_tab, text="3  전적")
        self.notebook.add(self.build_tab, text="4  빌드 적용")
        self.notebook.select(self.selection_tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel, add="+")
        for index in range(4):
            self.root.bind(
                f"<Control-Key-{index + 1}>",
                lambda _event, tab_index=index: self._select_tab(tab_index),
            )
        self.content = self.selection_content
        self._build_draft_panel()
        self._build_recommendations_panel()
        self._build_selection_detail_tabs()
        self._apply_codex_recommendation_visibility()
        self._build_play_panel()
        self._build_history_panel()
        self._build_build_panel()

    def _scroll_tab(
        self, parent: tk.Widget | None = None,
    ) -> tuple[tk.Frame, tk.Canvas, tk.Frame]:
        tab = tk.Frame(parent or self.notebook, bg=COLORS["bg"])
        canvas = tk.Canvas(tab, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            tab, orient="vertical", command=canvas.yview,
            style="Advisor.Vertical.TScrollbar",
        )
        content = tk.Frame(canvas, bg=COLORS["bg"])
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content.bind(
            "<Configure>",
            lambda _event, target=canvas: self._schedule_scrollregion_update(target),
        )
        canvas.bind("<Configure>", lambda e, c=canvas, w=content_window: c.itemconfigure(w, width=e.width))
        return tab, canvas, content

    def _schedule_scrollregion_update(
        self, canvas: tk.Canvas, delay_ms: int = 50,
    ) -> None:
        """Collapse geometry bursts and avoid redundant canvas bbox writes."""
        pending = self._scrollregion_after_ids.get(canvas)
        if pending:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                self._scrollregion_after_ids.pop(canvas, None)

        def update() -> None:
            self._scrollregion_after_ids.pop(canvas, None)
            try:
                bounds = canvas.bbox("all")
            except tk.TclError:
                return
            if bounds is None:
                bounds = (0, 0, 0, 0)
            normalized = tuple(int(value) for value in bounds)
            if self._scrollregion_bounds.get(canvas) == normalized:
                return
            self._scrollregion_bounds[canvas] = normalized
            try:
                canvas.configure(scrollregion=normalized)
            except tk.TclError:
                self._scrollregion_bounds.pop(canvas, None)

        self._scrollregion_after_ids[canvas] = self.root.after(delay_ms, update)

    def _select_tab(self, index: int) -> str:
        if hasattr(self, "notebook") and 0 <= index < self.notebook.index("end"):
            self.notebook.select(index)
        return "break"

    def _current_main_tab_index(self) -> int:
        if not hasattr(self, "notebook"):
            return 0
        try:
            return self.notebook.index(self.notebook.select())
        except tk.TclError:
            return self._active_main_tab_index

    def _on_mousewheel(self, event: tk.Event) -> str | None:
        try:
            top = event.widget.winfo_toplevel()
            detail_canvas = getattr(top, "_advisor_scroll_canvas", None)
            if detail_canvas and detail_canvas.winfo_exists():
                delta = getattr(event, "delta", 0)
                number = getattr(event, "num", 0)
                direction = -1 if delta > 0 or number == 4 else 1
                detail_canvas.yview_scroll(direction * 3, "units")
                return "break"
            # bind_all receives wheel events from every Toplevel. A dialog that
            # has no own scroll canvas must never move the main tab behind it.
            if top is not self.root:
                return "break"
        except tk.TclError:
            return "break"
        index = self.notebook.index(self.notebook.select())
        canvas = (
            self.play_canvas if index == 1 else
            self._active_player_history_canvas() if index == 2
            else self.selection_canvas
        )
        if index == 3:
            canvas = self.build_canvas
        delta = getattr(event, "delta", 0)
        number = getattr(event, "num", 0)
        direction = -1 if delta > 0 or number == 4 else 1
        canvas.yview_scroll(direction * 3, "units")
        return "break"

    def _on_rune_style_mousewheel(self, event: tk.Event) -> str:
        """Scroll the build page without changing a focused rune combobox."""
        self._on_mousewheel(event)
        # A widget-level ``break`` prevents ttk.Combobox's class binding from
        # moving to the previous/next option and also avoids a second bind_all
        # scroll for the same wheel event.
        return "break"

    def _on_tab_changed(self, _event: tk.Event | None = None) -> None:
        """Switch immediately, then prepare only the visible tab on the next frame."""
        if not hasattr(self, "play_tab"):
            return
        selected_index = self._current_main_tab_index()
        self._active_main_tab_index = selected_index
        if self._main_tab_activation_after_id:
            try:
                self.root.after_cancel(self._main_tab_activation_after_id)
            except tk.TclError:
                pass
        # Tk gets one paint opportunity before any DB check or large widget diff.
        self._main_tab_activation_after_id = self.root.after(
            35, lambda index=selected_index: self._activate_main_tab(index)
        )

    def _activate_main_tab(self, index: int) -> None:
        self._main_tab_activation_after_id = None
        if index != self._current_main_tab_index():
            return
        if index == 0:
            self._schedule_selection_render()
        elif index == 1:
            self._schedule_play_render()
        elif index == 2:
            self._activate_player_history_tab()
        elif index == 3:
            self._sync_build_selection_from_draft(self.draft)
            self._render_build()
            if not self.demo:
                request_key = (
                    self._build_selected_champion_id, self.draft.my_role
                )
                remaining = self._build_cooldown_remaining(*request_key)
                needs_statistics = bool(
                    self.build_guide
                    and not build_guide_has_statistics(self.build_guide)
                )
                statistics_due = (
                    self._build_statistics_upgrade_remaining(
                        *request_key
                    ).total_seconds() <= 0
                )
                if (
                    request_key not in self._tab_build_refresh_attempted
                    and (
                        not self.build_guide
                        or (
                            needs_statistics and statistics_due
                        )
                        or (
                            not needs_statistics
                            and remaining.total_seconds() <= 0
                        )
                    )
                ):
                    self._tab_build_refresh_attempted.add(request_key)
                    self.root.after(
                        90, lambda: self._refresh_build_guide(automatic=True)
                    )

    def _history_home_is_selected(self) -> bool:
        if not hasattr(self, "history_notebook"):
            return True
        try:
            return str(self.history_notebook.select()) == str(self.history_home_tab)
        except tk.TclError:
            return True

    def _active_player_history_canvas(self) -> tk.Canvas:
        if not hasattr(self, "history_notebook"):
            return self.history_canvas
        try:
            selected = str(self.history_notebook.select())
        except tk.TclError:
            return self.history_home_canvas
        key = self._player_history_tab_keys.get(selected, "")
        state = self._player_history_tabs.get(key)
        canvas = state.get("canvas") if state else None
        return canvas if isinstance(canvas, tk.Canvas) else self.history_home_canvas

    def _on_player_history_tab_changed(
        self, _event: tk.Event | None = None,
    ) -> None:
        if self._current_main_tab_index() == 2:
            self.root.after(35, self._activate_player_history_tab)

    def _activate_player_history_tab(self) -> None:
        if self._current_main_tab_index() != 2:
            return
        if self._history_home_is_selected():
            self._ensure_history_loaded()
            return
        try:
            selected = str(self.history_notebook.select())
        except tk.TclError:
            return
        key = self._player_history_tab_keys.get(selected, "")
        if key:
            state = self._player_history_tabs.get(key)
            if state is not None:
                state["last_opened_at"] = time.monotonic()
            self._ensure_player_history_page(key)

    def _owner_riot_id_key(self) -> str:
        game_name = self.storage.get_setting("riot_game_name")
        tag_line = self.storage.get_setting("riot_tag_line")
        return normalize_riot_id(
            f"{game_name}#{tag_line}" if game_name and tag_line else ""
        )

    def _open_player_history_tab(self, riot_id: str) -> None:
        parts = split_riot_id(riot_id)
        key = normalize_riot_id(riot_id)
        if not parts or not key:
            return
        self.notebook.select(self.history_tab)
        if key == self._owner_riot_id_key():
            self.history_notebook.select(self.history_home_tab)
            self.root.after(35, self._ensure_history_loaded)
            return
        existing = self._player_history_tabs.get(key)
        if existing:
            tab = existing.get("tab")
            if tab is not None:
                try:
                    existing["last_opened_at"] = time.monotonic()
                    self.history_notebook.select(tab)
                    return
                except tk.TclError:
                    self._player_history_tabs.pop(key, None)

        # Keep every dynamic tab reachable at the minimum window width. The
        # full Riot ID remains visible inside the page; only the least-recently
        # used extra tab is retired when the compact seven-tab strip is full.
        if len(self._player_history_tabs) >= 6:
            oldest_key = min(
                self._player_history_tabs,
                key=lambda item: float(
                    self._player_history_tabs[item].get("last_opened_at") or 0.0
                ),
            )
            self._close_player_history_tab(oldest_key)

        game_name, tag_line = parts
        self._player_history_generation += 1
        generation = self._player_history_generation
        tab, canvas, content = self._scroll_tab(self.history_notebook)
        short_name = game_name if len(game_name) <= 9 else game_name[:8] + "…"
        self.history_notebook.add(tab, text=short_name)
        state: dict[str, object] = {
            "key": key,
            "riot_id": f"{game_name}#{tag_line}",
            "game_name": game_name,
            "tag_line": tag_line,
            "tab": tab,
            "canvas": canvas,
            "content": content,
            "generation": generation,
            "last_opened_at": time.monotonic(),
            "pager": OtherPlayerHistoryPager(),
            "puuid": self.storage.find_puuid_by_riot_id(
                f"{game_name}#{tag_line}"
            ),
            "loading": False,
            "local_hydrating": False,
            "local_hydrated": False,
            "local_match_ids": [],
            "remote_confirmed": False,
            "cancel_event": threading.Event(),
            "profile_loading": False,
            "profile": None,
            "overview": None,
            "rendered_match_ids": set(),
            "champion_signature": "",
            "opgg_recent_signature": "",
            "riot_history_error": "",
        }
        self._player_history_tabs[key] = state
        self._player_history_tab_keys[str(tab)] = key
        self._build_player_history_page(state)
        self.history_notebook.select(tab)
        self.root.after(35, lambda selected=key: self._ensure_player_history_page(selected))

    def _close_player_history_tab(self, key: str) -> None:
        state = self._player_history_tabs.pop(key, None)
        if not state:
            return
        cancel_event = state.get("cancel_event")
        if isinstance(cancel_event, threading.Event):
            cancel_event.set()
        self._player_history_generation += 1
        tab = state.get("tab")
        canvas = state.get("canvas")
        if tab is not None:
            self._player_history_tab_keys.pop(str(tab), None)
        if isinstance(canvas, tk.Canvas):
            after_id = self._scrollregion_after_ids.pop(canvas, None)
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
            self._scrollregion_bounds.pop(canvas, None)
        try:
            if tab is not None:
                self.history_notebook.forget(tab)
                tab.destroy()
        except tk.TclError:
            pass
        if not self.history_notebook.select():
            self.history_notebook.select(self.history_home_tab)

    def _player_history_state_current(
        self, key: str, generation: int,
    ) -> dict[str, object] | None:
        state = self._player_history_tabs.get(key)
        if not state or int(state.get("generation") or -1) != generation:
            return None
        tab = state.get("tab")
        try:
            if tab is None or not bool(tab.winfo_exists()):
                return None
        except tk.TclError:
            return None
        return state

    def _build_player_history_page(self, state: dict[str, object]) -> None:
        content = state["content"]
        riot_id = str(state["riot_id"])
        panel = self._panel(content, "플레이어 솔로랭크 전적", COLORS["blue"])
        top = tk.Frame(panel, bg=COLORS["panel"])
        top.pack(fill="x", pady=(0, 10))
        tk.Label(
            top, text=riot_id, bg=COLORS["panel"], fg=COLORS["gold"],
            font=("Malgun Gothic", 14, "bold"), cursor="hand2",
        ).pack(side="left")
        self._button(
            top, "이 탭 닫기  ×",
            lambda selected=str(state["key"]): self._close_player_history_tab(selected),
            COLORS["red"], width=12,
        ).pack(side="right")
        status = tk.Label(
            top, text="저장된 정보 확인 중…", bg=COLORS["panel"],
            fg=COLORS["blue"], font=("Malgun Gothic", 8),
        )
        status.pack(side="left", padx=12)

        summary = tk.Frame(panel, bg=COLORS["panel"])
        summary.pack(fill="x", pady=(0, 10))
        metrics: dict[str, tuple[tk.Label, tk.Label]] = {}
        for index, (name, title, color) in enumerate((
            ("rank", "현재 솔로랭크", COLORS["gold"]),
            ("season", "이번 시즌", COLORS["green"]),
            ("loaded", "불러온 전적", COLORS["blue"]),
        )):
            outer, value, detail = self._mini_metric(summary, title, color)
            outer.pack(
                side="left", fill="x", expand=True,
                padx=(0 if index == 0 else 4, 4),
            )
            metrics[name] = (value, detail)

        champion_panel = tk.Frame(
            panel, bg=COLORS["panel_2"], padx=11, pady=9,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        champion_panel.pack(fill="x", pady=(0, 10))
        tk.Label(
            champion_panel, text="시즌 챔피언 성능", bg=COLORS["panel_2"],
            fg=COLORS["text"], font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w")
        champions = tk.Frame(champion_panel, bg=COLORS["panel_2"])
        champions.pack(fill="x", pady=(7, 0))

        matches_panel = tk.Frame(
            panel, bg=COLORS["panel_2"], padx=11, pady=9,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        matches_panel.pack(fill="x")
        tk.Label(
            matches_panel, text="최근 솔로랭크 · 10경기 단위", bg=COLORS["panel_2"],
            fg=COLORS["text"], font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w", pady=(0, 6))
        matches = tk.Frame(matches_panel, bg=COLORS["panel_2"])
        matches.pack(fill="x")
        more = self._button(
            matches_panel, "처음 10경기 불러오기",
            lambda selected=str(state["key"]): self._load_more_player_history(selected),
            COLORS["purple"], width=24,
        )
        more.pack(anchor="center", pady=(9, 0))
        state.update({
            "status_label": status,
            "metrics": metrics,
            "champions_frame": champions,
            "matches_frame": matches,
            "more_button": more,
        })

    def _ensure_player_history_page(self, key: str) -> None:
        state = self._player_history_tabs.get(key)
        if not state:
            return
        # Selecting an already populated player tab must be a pure view switch.
        # In particular, do not start another profile/page refresh or replace
        # the existing ten cards just because the user briefly viewed 내 전적.
        rendered = state.get("rendered_match_ids")
        if isinstance(rendered, set) and rendered and not bool(state.get("loading")):
            return
        self._ensure_player_history_profile(key)
        if not bool(state.get("local_hydrated")):
            self._hydrate_player_history_cache(key)
            return
        pager = state.get("pager")
        if (
            isinstance(pager, OtherPlayerHistoryPager)
            and not pager.match_ids
            and not bool(state.get("remote_confirmed"))
        ):
            self._load_more_player_history(key)
        else:
            self._render_player_history_matches(state)

    def _hydrate_player_history_cache(self, key: str) -> None:
        """Paint at most ten saved games before any Riot network request."""
        state = self._player_history_tabs.get(key)
        if (
            not state
            or bool(state.get("local_hydrated"))
            or bool(state.get("local_hydrating"))
        ):
            return
        generation = int(state.get("generation") or 0)
        riot_id = str(state.get("riot_id") or "")
        cancel_event = state.get("cancel_event")
        max_age = min(
            self._request_max_age("player_analysis_cooldown_hours"),
            timedelta(days=1),
        )
        state["local_hydrating"] = True
        status = state.get("status_label")
        if isinstance(status, tk.Label):
            status.configure(text="저장된 최근 10경기 확인 중…", fg=COLORS["blue"])

        def work() -> tuple[
            str, HistoryOverview, list[str], bool, bool,
        ] | None:
            if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                return None
            fresh = self.storage.load_player_match_page(
                riot_id, 0, max_age=max_age,
            )
            cached = fresh or self.storage.load_player_match_page(
                riot_id, 0, max_age=timedelta(days=1),
            )
            if not cached:
                return "", HistoryOverview(), [], False, False
            puuid, match_ids, has_more, _updated_at = cached
            matches = [
                payload for match_id in match_ids
                if (payload := self.storage.load_match(match_id)) is not None
            ]
            overview = analyze_history(matches, puuid, limit=10)
            return puuid, overview, match_ids, has_more, fresh is not None

        def success(
            result: tuple[
                str, HistoryOverview, list[str], bool, bool,
            ] | None,
        ) -> None:
            current = self._player_history_state_current(key, generation)
            if not current:
                return
            current["local_hydrating"] = False
            current["local_hydrated"] = True
            fresh_page = False
            if result is not None:
                puuid, overview, match_ids, has_more, fresh_page = result
                if puuid:
                    current["puuid"] = puuid
                current["overview"] = overview
                if fresh_page:
                    pager = current.get("pager")
                    if isinstance(pager, OtherPlayerHistoryPager):
                        pager.accept_page(match_ids, has_more)
                    current["remote_confirmed"] = True
                    current["local_match_ids"] = []
                else:
                    current["local_match_ids"] = match_ids
                self._render_player_history_matches(current)
            # The visible local page remains usable if the key is unavailable;
            # when it is valid this confirms the exact newest Riot page.
            if not fresh_page:
                self._load_more_player_history(key)

        def error(_exc: Exception) -> None:
            current = self._player_history_state_current(key, generation)
            if not current:
                return
            current["local_hydrating"] = False
            current["local_hydrated"] = True
            self._load_more_player_history(key)

        self._background(work, success, error)

    def _load_more_player_history(self, key: str) -> None:
        state = self._player_history_tabs.get(key)
        if not state or bool(state.get("loading")):
            return
        pager = state.get("pager")
        if not isinstance(pager, OtherPlayerHistoryPager):
            return
        request_page = pager.next_request()
        more_button = state.get("more_button")
        if request_page is None:
            if isinstance(more_button, tk.Button):
                more_button.configure(text="불러온 솔로랭크 마지막 경기", state="disabled")
            return
        start, count = request_page
        game_name = str(state.get("game_name") or "")
        tag_line = str(state.get("tag_line") or "")
        riot_id = str(state.get("riot_id") or "")
        generation = int(state.get("generation") or 0)
        existing_ids = list(pager.match_ids)
        cancel_event = state.get("cancel_event")
        api_key = self.storage.get_setting("riot_api_key")
        api_key_valid = bool(api_key) and not self.storage.riot_api_key_needs_refresh()
        max_age = min(
            self._request_max_age("player_analysis_cooldown_hours"),
            timedelta(days=1),
        )
        state["loading"] = True
        if isinstance(more_button, tk.Button):
            more_button.configure(text="다음 10경기 불러오는 중…", state="disabled")
        status = state.get("status_label")
        if isinstance(status, tk.Label):
            status.configure(
                text=f"솔로랭크 {start + 1}~{start + count}경기 요청 중…",
                fg=COLORS["blue"],
            )

        def work() -> tuple[
            str, list[str], int, bool, HistoryOverview, str,
        ] | None:
            # Serialize explicit player pages so opening several tabs cannot
            # burst through Riot's development-key request budget. A tab can
            # be closed while waiting behind the owner's large sync, so check
            # its private cancellation event again after acquiring the lane.
            if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                return None
            fresh = self.storage.load_player_match_page(
                riot_id, start, max_age=max_age,
            )
            stale = fresh or self.storage.load_player_match_page(
                riot_id, start, max_age=timedelta(days=1),
            )
            source = "CACHE" if fresh else ""
            if fresh:
                puuid, page_ids, has_more, _updated_at = fresh
                saved = 0
            elif not api_key_valid and stale:
                puuid, page_ids, has_more, _updated_at = stale
                saved = 0
                source = "STALE"
            elif not api_key_valid:
                raise RiotApiError(
                    "Riot API 키를 갱신하면 다음 10경기를 불러올 수 있습니다."
                )
            else:
                puuid = ""
                page_ids = []
                has_more = False
                saved = 0
            if not source:
                with self._riot_history_request_lock:
                    if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                        return None
                    puuid, page_ids, saved, has_more = RiotApiClient(
                        api_key
                    ).sync_player_match_page(
                        self.storage, game_name, tag_line, start=start, count=count,
                        known_puuid=(
                            str(state.get("puuid") or "")
                            or self.storage.find_puuid_by_riot_id(riot_id)
                        ),
                    )
                    source = "REMOTE"
            combined_ids = list(dict.fromkeys([*existing_ids, *page_ids]))
            payloads = [
                payload for match_id in combined_ids
                if (payload := self.storage.load_match(match_id)) is not None
            ]
            return (
                puuid,
                page_ids,
                saved,
                has_more,
                analyze_history(payloads, puuid),
                source,
            )

        def success(
            result: tuple[
                str, list[str], int, bool, HistoryOverview, str,
            ] | None,
        ) -> None:
            current = self._player_history_state_current(key, generation)
            if not current:
                return
            current["loading"] = False
            if result is None:
                return
            puuid, page_ids, saved, has_more, overview, source = result
            current["puuid"] = puuid
            current["overview"] = overview
            current["remote_confirmed"] = True
            if start == 0:
                # A stale local preview may not be the exact current first
                # Riot page. Replace only this tab's match list once, without
                # touching the main history or any play card.
                preview_ids = list(current.get("local_match_ids") or [])
                if page_ids != preview_ids:
                    frame = current.get("matches_frame")
                    rendered = current.get("rendered_match_ids")
                    if isinstance(frame, tk.Frame):
                        self._clear(frame)
                    if isinstance(rendered, set):
                        rendered.clear()
                current["local_match_ids"] = []
            current_pager = current.get("pager")
            if isinstance(current_pager, OtherPlayerHistoryPager):
                current_pager.accept_page(page_ids, has_more)
            self._render_player_history_matches(
                current, saved=saved, source=source,
            )

        def error(exc: Exception) -> None:
            current = self._player_history_state_current(key, generation)
            if not current:
                return
            current["loading"] = False
            message = str(exc)
            account_unavailable = (
                isinstance(exc, RiotApiError)
                and (
                    "Riot ID를 찾지 못했습니다" in message
                    or "HTTP 404" in message
                )
            )
            if account_unavailable:
                current["riot_history_error"] = message
                current["remote_confirmed"] = True
                rendered = current.get("rendered_match_ids")
                if isinstance(rendered, set) and rendered:
                    label = current.get("status_label")
                    if isinstance(label, tk.Label):
                        label.configure(
                            text=self._tr(
                                "저장된 Riot 전적 유지 · 계정 재조회 불가"
                            ),
                            fg=COLORS["orange"],
                        )
                else:
                    self._render_player_history_opgg_fallback(current)
                button = current.get("more_button")
                if isinstance(button, tk.Button):
                    button.configure(
                        text=self._tr("Riot 상세 전적 이용 불가"),
                        state="disabled",
                    )
                return
            label = current.get("status_label")
            if isinstance(label, tk.Label):
                label.configure(text=f"전적 갱신 실패 · {exc}", fg=COLORS["red"])
            button = current.get("more_button")
            if isinstance(button, tk.Button):
                button.configure(text="10경기 다시 불러오기", state="normal")

        self._background(work, success, error)

    def _ensure_player_history_profile(self, key: str) -> None:
        state = self._player_history_tabs.get(key)
        if not state:
            return
        if key in self._player_history_profile_inflight:
            generation = int(state.get("generation") or 0)
            self.root.after(
                300,
                lambda selected=key, expected=generation: (
                    self._ensure_player_history_profile(selected)
                    if self._player_history_state_current(selected, expected)
                    else None
                ),
            )
            return
        if bool(state.get("profile_loading")):
            return
        generation = int(state.get("generation") or 0)
        game_name = str(state.get("game_name") or "")
        tag_line = str(state.get("tag_line") or "")
        riot_id = str(state.get("riot_id") or "")
        cancel_event = state.get("cancel_event")
        state["profile_loading"] = True
        self._player_history_profile_inflight.add(key)
        profile_max_age = min(
            self._request_max_age("player_analysis_cooldown_hours"),
            timedelta(days=1),
        )

        def work() -> OpggMcpSummonerProfile | None:
            fresh = self.storage.load_opgg_player_profile(
                riot_id, max_age=profile_max_age,
            )
            cached = fresh or self.storage.load_opgg_player_profile(
                riot_id, max_age=timedelta(days=1),
            )
            if cached:
                self._post_ui(
                    lambda value=cached: self._apply_player_history_profile(
                        key, generation, value,
                    )
                )
            if fresh and str(fresh.recent_matches_status or "").upper() in {
                "OK", "EMPTY",
            }:
                return fresh
            if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                return None
            with self._player_history_profile_semaphore:
                if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                    return None
                client = OpggMcpClient(timeout=15.0)
                profile = client.summoner_profile(
                    game_name, tag_line, region="KR", lang="ko_KR",
                )
                try:
                    profile.recent_matches = client.summoner_recent_matches(
                        game_name, tag_line, region="KR", lang="ko_KR", limit=10,
                    )
                    profile.recent_matches_status = (
                        "OK" if profile.recent_matches else "EMPTY"
                    )
                except OpggMcpError:
                    if cached:
                        profile.recent_matches = list(cached.recent_matches)
                        profile.recent_matches_status = cached.recent_matches_status
                    else:
                        profile.recent_matches_status = "ERROR"
            self.storage.save_opgg_player_profile(profile)
            return profile

        def success(profile: OpggMcpSummonerProfile | None) -> None:
            self._player_history_profile_inflight.discard(key)
            current = self._player_history_tabs.get(key)
            if not current:
                return
            current["profile_loading"] = False
            if profile:
                self._apply_player_history_profile(
                    key, int(current.get("generation") or 0), profile,
                )

        def error(_exc: Exception) -> None:
            self._player_history_profile_inflight.discard(key)
            current = self._player_history_tabs.get(key)
            if current:
                current["profile_loading"] = False

        self._background(work, success, error)

    def _apply_player_history_profile(
        self, key: str, generation: int, profile: OpggMcpSummonerProfile,
    ) -> None:
        state = self._player_history_state_current(key, generation)
        if not state:
            return
        state["profile"] = profile
        self._render_player_history_profile(state)
        if state.get("riot_history_error"):
            self._render_player_history_opgg_fallback(state)

    def _render_player_history_profile(self, state: dict[str, object]) -> None:
        profile = state.get("profile")
        metrics = state.get("metrics")
        if not isinstance(profile, OpggMcpSummonerProfile) or not isinstance(metrics, dict):
            return
        rank_value, rank_detail = metrics["rank"]
        season_value, season_detail = metrics["season"]
        rank_value.configure(
            text=(
                f"{profile.tier} {profile.division}"
                if profile.tier != "UNRANKED" else "언랭크"
            )
        )
        rank_detail.configure(
            text=f"{profile.league_points} LP" if profile.tier != "UNRANKED" else "시즌 기록 없음"
        )
        games = profile.season_wins + profile.season_losses
        season_value.configure(text=f"{profile.season_wins}승 {profile.season_losses}패")
        season_detail.configure(
            text=f"{profile.season_wins / games * 100:.1f}%" if games else "승률 --"
        )
        signature = repr(tuple(
            (row.champion_key, row.games, row.wins, row.losses)
            for row in profile.champion_stats[:10]
        ))
        if signature == str(state.get("champion_signature") or ""):
            return
        state["champion_signature"] = signature
        frame = state.get("champions_frame")
        if not isinstance(frame, tk.Frame):
            return
        self._clear(frame)
        if not profile.champion_stats:
            tk.Label(
                frame, text="시즌 챔피언 표본 없음", bg=COLORS["panel_2"],
                fg=COLORS["muted"], font=("Malgun Gothic", 7),
            ).pack(anchor="w")
            return
        for column in range(5):
            frame.grid_columnconfigure(column, weight=1, uniform="player_history_champion")
        for index, row in enumerate(profile.champion_stats[:10]):
            champion_id, ko_name = self.registry.from_key(row.champion_key)
            card = tk.Frame(frame, bg=COLORS["surface"], padx=7, pady=5)
            card.grid(
                row=index // 5, column=index % 5, sticky="ew",
                padx=(0, 4), pady=(0, 4),
            )
            icon_label = tk.Label(
                card, text=ko_name[:1], bg=COLORS["chip"], fg=COLORS["gold"],
                width=28, font=("Malgun Gothic", 8, "bold"),
            )
            icon_label.pack(side="left", padx=(0, 5))
            def apply_icon(
                label: tk.Label = icon_label, value: str = champion_id,
            ) -> None:
                try:
                    image_value = self.icon_cache.get(value, 28)
                    if label.winfo_exists() and image_value:
                        label.configure(image=image_value, text="", width=0)
                except tk.TclError:
                    return

            image = self.icon_cache.get(champion_id, 28, apply_icon)
            if image:
                icon_label.configure(image=image, text="", width=0)
            losses = max(row.games - row.wins, 0)
            tk.Label(
                card,
                text=f"{ko_name}\n{row.games}판 · {row.wins}승 {losses}패",
                bg=COLORS["surface"], fg=COLORS["text"], justify="left",
                font=("Malgun Gothic", 7, "bold"),
            ).pack(side="left")

    def _render_player_history_opgg_fallback(
        self, state: dict[str, object],
    ) -> None:
        """Show OP.GG's bounded recent form when Riot hides the account ID."""
        profile = state.get("profile")
        frame = state.get("matches_frame")
        status = state.get("status_label")
        metrics = state.get("metrics")
        if not isinstance(frame, tk.Frame):
            return
        if not isinstance(profile, OpggMcpSummonerProfile):
            if isinstance(status, tk.Label):
                status.configure(
                    text=self._tr("OP.GG 최근 경기 확인 중…"),
                    fg=COLORS["orange"],
                )
            return

        matches = opgg_player_history_matches(profile)
        signature = repr(tuple(
            (
                match.match_id, match.created_at, match.champion_key,
                match.result, match.kills, match.deaths, match.assists,
                match.op_score, match.op_score_rank,
            )
            for match in matches
        ))
        if signature == str(state.get("opgg_recent_signature") or ""):
            return
        state["opgg_recent_signature"] = signature
        self._clear(frame)

        if isinstance(metrics, dict):
            loaded_value, loaded_detail = metrics["loaded"]
            loaded_value.configure(text=f"{len(matches)}경기")
            loaded_detail.configure(text=self._tr("OP.GG 대체 기록"))
        if isinstance(status, tk.Label):
            status.configure(
                text=self._tr(
                    "Riot 계정 조회 불가 · OP.GG 최근 솔로랭크 표시"
                ),
                fg=COLORS["orange"],
            )

        tk.Label(
            frame,
            text=self._tr(
                "OP.GG 대체 기록 · 아이템·룬·양 팀 상세 정보 없음"
            ),
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7),
        ).pack(anchor="w", pady=(0, 6))
        if not matches:
            message = (
                "OP.GG 최근 경기 조회 실패"
                if profile.recent_matches_status == "ERROR"
                else "OP.GG 최근 솔로랭크 기록 없음"
            )
            tk.Label(
                frame, text=self._tr(message), bg=COLORS["surface"],
                fg=COLORS["muted"], font=("Malgun Gothic", 8),
                padx=10, pady=14,
            ).pack(fill="x")
            return

        for match in matches:
            won = str(match.result or "").upper() == "WIN"
            accent, card_bg, _badge_bg = history_result_style(won)
            outer = tk.Frame(frame, bg=accent, padx=2, pady=2)
            outer.pack(fill="x", pady=3)
            card = tk.Frame(outer, bg=card_bg, padx=9, pady=7)
            card.pack(fill="x")
            champion_id, korean_name = self.registry.from_key(match.champion_key)
            icon_label = tk.Label(
                card, text=self._champion_text(champion_id, korean_name)[:1],
                bg=COLORS["chip"], fg=COLORS["gold"], width=42,
                font=("Malgun Gothic", 11, "bold"), highlightthickness=1,
                highlightbackground=accent,
            )
            icon_label.pack(side="left", padx=(0, 9))

            def apply_icon(
                label: tk.Label = icon_label, value: str = champion_id,
            ) -> None:
                try:
                    image_value = self.icon_cache.get(value, 42)
                    if label.winfo_exists() and image_value:
                        label.configure(image=image_value, text="", width=0)
                except tk.TclError:
                    return

            image = self.icon_cache.get(champion_id, 42, apply_icon)
            if image:
                icon_label.configure(image=image, text="", width=0)

            result_text = self._tr("승리" if won else "패배")
            champion_text = self._champion_text(champion_id, korean_name)
            created_text = str(match.created_at or "").replace("T", " ")[:16]
            tk.Label(
                card, text=f"{result_text} · {champion_text}",
                bg=card_bg, fg=accent, width=22, anchor="w",
                font=("Malgun Gothic", 9, "bold"),
            ).pack(side="left")
            tk.Label(
                card,
                text=(
                    f"{match.kills}/{match.deaths}/{match.assists}  ·  "
                    f"KDA {(match.kills + match.assists) / max(match.deaths, 1):.2f}"
                ),
                bg=card_bg, fg=COLORS["text"], width=27, anchor="w",
                font=("Malgun Gothic", 8, "bold"),
            ).pack(side="left")
            tk.Label(
                card, text=self._position_text(match.position),
                bg=card_bg, fg=COLORS["blue"], width=12, anchor="w",
                font=("Malgun Gothic", 8),
            ).pack(side="left")
            score_text = (
                f"OP {match.op_score:.1f} · {match.op_score_rank}{self._tr('등')}"
                if match.op_score > 0 else self._tr("OP 점수 없음")
            )
            tk.Label(
                card, text=score_text, bg=card_bg, fg=COLORS["gold"],
                width=22, anchor="w", font=("Malgun Gothic", 8),
            ).pack(side="left")
            tk.Label(
                card, text=created_text or self._tr("시간 미제공"),
                bg=card_bg, fg=COLORS["muted"], anchor="e",
                font=("Malgun Gothic", 7),
            ).pack(side="right")

    def _render_player_history_matches(
        self,
        state: dict[str, object],
        *,
        saved: int = 0,
        source: str = "",
    ) -> None:
        pager = state.get("pager")
        overview = state.get("overview")
        metrics = state.get("metrics")
        if not isinstance(pager, OtherPlayerHistoryPager):
            return
        local_match_ids = list(state.get("local_match_ids") or [])
        loaded_count = len(pager.match_ids) if pager.match_ids else len(local_match_ids)
        if isinstance(metrics, dict):
            loaded_value, loaded_detail = metrics["loaded"]
            loaded_value.configure(text=f"{loaded_count}경기")
            loaded_detail.configure(
                text="저장본" if not pager.match_ids and local_match_ids else "요청당 최대 10경기"
            )
        frame = state.get("matches_frame")
        rendered = state.get("rendered_match_ids")
        if isinstance(frame, tk.Frame) and isinstance(rendered, set) and isinstance(overview, HistoryOverview):
            for entry in overview.entries:
                if entry.match_id in rendered:
                    continue
                self._render_history_match(
                    entry,
                    parent=frame,
                    perspective_puuid=str(state.get("puuid") or ""),
                )
                rendered.add(entry.match_id)
        status = state.get("status_label")
        if isinstance(status, tk.Label):
            if pager.match_ids:
                status.configure(
                    text=(
                        f"솔로랭크 {len(pager.match_ids)}경기 · 신규 저장 {saved} · "
                        "로컬 캐시 우선"
                    ),
                    fg=COLORS["green"],
                )
            elif local_match_ids:
                status.configure(
                    text=f"저장된 솔로랭크 {len(local_match_ids)}경기 먼저 표시",
                    fg=COLORS["green"],
                )
            elif bool(state.get("remote_confirmed")):
                status.configure(
                    text="확인된 솔로랭크 기록 없음",
                    fg=COLORS["muted"],
                )
        button = state.get("more_button")
        if isinstance(button, tk.Button):
            if pager.match_ids:
                button.configure(
                    text=(
                        "10경기 더 불러오기" if pager.has_more
                        else "불러온 솔로랭크 마지막 경기"
                    ),
                    state="normal" if pager.has_more else "disabled",
                )
            elif bool(state.get("remote_confirmed")):
                button.configure(
                    text="불러온 솔로랭크 마지막 경기",
                    state="disabled",
                )

    def _panel(
        self, parent: tk.Widget, title: str, accent: str | None = None,
        *, outer_padx: int = 22,
        outer_pady: tuple[int, int] = (0, 11),
    ) -> tk.Frame:
        outer = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        outer.pack(fill="x", padx=outer_padx, pady=outer_pady)
        inner = tk.Frame(outer, bg=COLORS["panel"], padx=18, pady=13)
        inner.pack(fill="both", expand=True)
        heading = tk.Frame(inner, bg=COLORS["panel"])
        heading.pack(fill="x", pady=(0, 11))
        marker = tk.Frame(heading, bg=accent or COLORS["blue"], width=4, height=20)
        marker.pack(side="left", padx=(0, 9))
        marker.pack_propagate(False)
        title_label = tk.Label(
            heading, text=self._tr(title), bg=COLORS["panel"], fg=COLORS["text"],
            font=("Malgun Gothic", 11, "bold"),
        )
        title_label.pack(side="left")
        setattr(inner, "_advisor_title_label", title_label)
        tk.Frame(inner, bg=COLORS["border"], height=1).pack(fill="x", pady=(0, 11))
        return inner

    def _build_header(self, parent: tk.Widget) -> None:
        header = tk.Frame(parent, bg=COLORS["bg"], padx=24, pady=9)
        header.pack(fill="x")
        self.header_frame = header
        top = tk.Frame(header, bg=COLORS["bg"])
        top.pack(fill="x")
        left = tk.Frame(top, bg=COLORS["bg"])
        left.pack(side="left", fill="x", expand=True)
        self.app_title_label = tk.Label(
            left, text="LOL PICK ADVISOR", bg=COLORS["bg"], fg=COLORS["gold"],
            font=("Malgun Gothic", 18, "bold"),
        )
        self.app_title_label.pack(anchor="w")
        self.connection_label = tk.Label(
            left, text="롤 클라이언트 확인 중", bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 10),
        )
        self.connection_label.pack(anchor="w", pady=(3, 0))
        actions = tk.Frame(top, bg=COLORS["bg"])
        actions.pack(side="right")
        self.opgg_header_label = tk.Label(
            actions, text="OP.GG 캐시 없음", bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9),
        )
        self.opgg_header_label.grid(row=1, column=2, columnspan=2, sticky="e", pady=(6, 0))
        self.auto_accept_button = self._button(
            actions, "자동 수락 OFF", self._toggle_auto_accept, COLORS["green"]
        )
        self.auto_accept_button.grid(row=0, column=0, padx=(0, 7))
        self.lux_auto_ban_button = self._button(
            actions, "자동 밴 OFF", self._toggle_lux_auto_ban, COLORS["red"]
        )
        self.lux_auto_ban_button.grid(row=0, column=1, padx=(0, 7))
        self.automation_status_label = tk.Label(
            actions, text="", bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"),
        )
        self.automation_status_label.grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        self.opgg_button = self._button(
            actions, "메타·상성·조합 갱신", self._refresh_opgg, COLORS["blue"]
        )
        self.opgg_button.grid(row=0, column=2, padx=(0, 7))
        self.riot_button = self._button(
            actions, "전적 갱신", self._sync_riot, COLORS["green"]
        )
        self.riot_button.grid(row=0, column=3, padx=(0, 7))
        self.cache_manager_button = self._button(
            actions, "캐시 관리", self._open_cache_manager, COLORS["purple"]
        )
        self.cache_manager_button.grid(row=0, column=4, padx=(0, 7))
        self.settings_button = self._button(actions, "Riot 설정", self._open_settings, COLORS["muted"])
        self.settings_button.grid(row=0, column=5, padx=(0, 7))
        self.api_key_status_label = tk.Label(
            actions, text="", bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"),
        )
        self.api_key_status_label.grid(row=1, column=5, columnspan=2, sticky="e", pady=(6, 0))
        self.developer_portal_button = self._button(
            actions, "API 키 발급", self._open_developer_portal, COLORS["orange"]
        )
        self.developer_portal_button.grid(row=0, column=6, sticky="e")

        metrics = tk.Frame(header, bg=COLORS["bg"])
        metrics.pack(fill="x", pady=(10, 0))
        self.header_metrics_frame = metrics
        self.header_metrics: dict[str, tuple[tk.Label, tk.Label]] = {}
        metric_specs = (
            ("phase", "현재 단계", COLORS["gold"]),
            ("draft", "추천 기준", COLORS["blue"]),
            ("cache", "로컬 전적 DB", COLORS["green"]),
            ("data", "데이터 상태", COLORS["purple"]),
        )
        for index, (key, title, accent) in enumerate(metric_specs):
            outer, value, detail = self._metric_card(metrics, title, accent)
            self.header_metrics[key] = (value, detail)
            outer.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 5, 5))
        self.legal_notice_label = tk.Label(
            header,
            text=("비공식 개인용 도구 · 자동 수락/자동 밴은 기본 OFF · 픽/밴/빌드는 명시적 버튼으로만 변경 · "
                  "Riot Games가 보증하거나 공식 지원하지 않습니다."),
            bg=COLORS["bg"], fg="#5f6c82", font=("Malgun Gothic", 7),
        )
        self.legal_notice_label.pack(anchor="w", pady=(6, 0))

    def _metric_card(
        self, parent: tk.Widget, title: str, accent: str
    ) -> tuple[tk.Frame, tk.Label, tk.Label]:
        outer = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        card = tk.Frame(outer, bg=COLORS["surface"], padx=11, pady=6)
        card.pack(fill="both", expand=True)
        tk.Frame(card, bg=accent, width=3).pack(side="left", fill="y", padx=(0, 9))
        text = tk.Frame(card, bg=COLORS["surface"])
        text.pack(side="left", fill="x", expand=True)
        tk.Label(
            text, text=self._tr(title), bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w")
        value = tk.Label(
            text, text="--", bg=COLORS["surface"], fg=accent,
            font=("Malgun Gothic", 11, "bold"),
        )
        value.pack(anchor="w")
        detail = tk.Label(
            text, text="", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7),
        )
        detail.pack(anchor="w")
        return outer, value, detail

    def _button(
        self, parent: tk.Widget, text: str, command: Callable[[], None], color: str,
        width: int | None = None, filled: bool = False,
    ) -> tk.Button:
        base_bg = BUTTON_FILLS.get(color, COLORS["chip"]) if filled else COLORS["chip"]
        foreground = COLORS["text"] if filled else color
        button = tk.Button(
            parent, text=self._tr(text), command=command, bg=base_bg, fg=foreground,
            activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"], relief="flat",
            bd=0, padx=14, pady=8, width=width, cursor="hand2",
            highlightthickness=1, highlightbackground=color if filled else COLORS["border"],
            highlightcolor=color,
            font=("Malgun Gothic", 9, "bold"), disabledforeground="#5d6a7e",
        )
        setattr(button, "_advisor_base_bg", base_bg)
        setattr(button, "_advisor_hover_bg", COLORS["surface_hover"] if not filled else color)
        button.bind(
            "<Enter>",
            lambda _e, widget=button: widget.configure(
                bg=getattr(widget, "_advisor_hover_bg", COLORS["surface_hover"])
            )
            if str(widget.cget("state")) != "disabled" else None,
        )
        button.bind(
            "<Leave>",
            lambda _e, widget=button: widget.configure(
                bg=getattr(widget, "_advisor_base_bg", COLORS["chip"])
            )
            if str(widget.cget("state")) != "disabled" else None,
        )
        return button

    @staticmethod
    def _set_button_selected(
        button: tk.Button, selected: bool, accent: str,
    ) -> None:
        base_bg = COLORS["surface_selected"] if selected else COLORS["chip"]
        setattr(button, "_advisor_base_bg", base_bg)
        setattr(button, "_advisor_hover_bg", "#2a527d" if selected else COLORS["surface_hover"])
        button.configure(
            bg=base_bg,
            fg=COLORS["text"] if selected else accent,
            highlightbackground=accent if selected else COLORS["border"],
        )

    def _audit_lux_auto_ban(self, event: str, **details: object) -> None:
        """Persist sparse, token-free controller events for live diagnosis."""
        path = getattr(self, "_lux_auto_ban_audit_path", None)
        lock = getattr(self, "_lux_auto_ban_audit_lock", None)
        if not isinstance(path, Path) or lock is None:
            return
        payload = {
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "event": str(event),
            **{
                str(key): value
                for key, value in details.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            },
        }
        try:
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            # Diagnostics must never interfere with the actual LCU action.
            return

    def _audit_live_identity(self, event: str, **details: object) -> None:
        """Record counts and outcomes without persisting player identifiers."""
        path = getattr(self, "_live_identity_audit_path", None)
        lock = getattr(self, "_live_identity_audit_lock", None)
        if not isinstance(path, Path) or lock is None:
            return
        payload = {
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "event": str(event),
            **{
                str(key): value for key, value in details.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            },
        }
        try:
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            return

    def _toggle_auto_accept(self) -> None:
        if getattr(self, "demo", False):
            return
        self.auto_accept_enabled = not self.auto_accept_enabled
        self.storage.set_setting(
            "auto_accept_enabled", "1" if self.auto_accept_enabled else "0"
        )
        with self._auto_accept_lock:
            self._auto_accept_generation += 1
            self._auto_accept_cancel.set()
            self._auto_accept_cancel = threading.Event()
            self._auto_accept_monitoring = False
            self._auto_accept_cycle_seen = False
            self._auto_accept_deadline = 0.0
            self._auto_accept_status = (
                "게임 수락 대기" if self.auto_accept_enabled else "사용 안 함"
            )
        self._lux_auto_ban_watcher_wake.set()
        self._render_automation_toggles()

    def _auto_accept_monitor_is_current(self, generation: int) -> bool:
        with self._auto_accept_lock:
            return bool(
                self.auto_accept_enabled
                and self._auto_accept_monitoring
                and self._auto_accept_generation == generation
                and not self._auto_accept_cancel.is_set()
            )

    def _post_auto_accept_status(self, generation: int, status: str) -> None:
        with self._auto_accept_lock:
            if generation != self._auto_accept_generation:
                return
            self._auto_accept_status = status

        def apply() -> None:
            with self._auto_accept_lock:
                if generation != self._auto_accept_generation:
                    return
            self._render_automation_toggles()

        self._post_ui(apply)

    def _reset_auto_accept_cycle(self) -> None:
        with self._auto_accept_lock:
            if self._auto_accept_monitoring:
                self._auto_accept_generation += 1
                self._auto_accept_cancel.set()
                self._auto_accept_monitoring = False
            self._auto_accept_cycle_seen = False
            self._auto_accept_deadline = 0.0
            if self.auto_accept_enabled:
                self._auto_accept_status = "게임 수락 대기"

    def _ensure_auto_accept_monitor(self) -> bool:
        if not self.auto_accept_enabled or self.demo:
            return False
        delay = choose_auto_accept_delay_seconds()
        now = time.monotonic()
        with self._auto_accept_lock:
            if self._auto_accept_cycle_seen or self._auto_accept_monitoring:
                return False
            self._auto_accept_generation += 1
            generation = self._auto_accept_generation
            cancel = threading.Event()
            self._auto_accept_cancel = cancel
            self._auto_accept_monitoring = True
            self._auto_accept_cycle_seen = True
            self._auto_accept_deadline = now + delay
            self._auto_accept_status = f"약 {delay:.1f}초 후 수락 예정"
        self._post_auto_accept_status(
            generation, f"약 {delay:.1f}초 후 수락 예정",
        )
        threading.Thread(
            target=self._run_auto_accept_monitor,
            args=(generation, cancel, delay),
            name="auto-accept-delay",
            daemon=True,
        ).start()
        return True

    def _run_auto_accept_monitor(
        self, generation: int, cancel: threading.Event, delay: float,
    ) -> None:
        if cancel.wait(max(0.0, delay)):
            return
        if not self._auto_accept_monitor_is_current(generation):
            return
        try:
            accepted = self.lcu.accept_ready_check_if_pending(
                pre_commit_check=lambda: self._auto_accept_monitor_is_current(
                    generation
                ),
            )
        except LcuActionStateChanged:
            accepted = False
            status = "수락 상태 변경 · 다음 게임 대기"
        except LcuUnavailable as exc:
            accepted = False
            status = f"자동 수락 실패 · {exc}"
        else:
            status = "게임 수락 완료" if accepted else "수락 상태 변경 · 다음 게임 대기"
        with self._auto_accept_lock:
            if generation != self._auto_accept_generation:
                return
            self._auto_accept_monitoring = False
            self._auto_accept_deadline = 0.0
        self._post_auto_accept_status(generation, status)

    def _auto_ban_champion(
        self, champion_key: int | None = None,
    ) -> tuple[int, str, str]:
        """Return the persisted auto-ban target with a safe Lux fallback."""
        key = int(
            champion_key
            if champion_key is not None
            else getattr(self, "auto_ban_champion_key", 99) or 99
        )
        registry = getattr(self, "registry", None)
        if registry is None:
            return 99, "Lux", "럭스"
        if key not in registry.by_key:
            key = 99
        champion_id, champion_name_ko = registry.from_key(key)
        return key, champion_id, champion_name_ko

    def _set_auto_ban_champion(self, champion_key: int) -> None:
        key = int(champion_key)
        if key not in self.registry.by_key:
            key = 99
        if key == getattr(self, "auto_ban_champion_key", 99):
            return
        self.auto_ban_champion_key = key
        self.storage.set_setting("auto_ban_champion_key", str(key))
        self._reset_lux_auto_ban_schedule()
        self._lux_auto_ban_status = "내 밴 차례 대기"
        self._lux_auto_ban_watcher_wake.set()
        self._audit_lux_auto_ban(
            "target_changed", champion_key=key,
            champion_id=self.registry.from_key(key)[0],
        )
        self._render_automation_toggles()

    def _toggle_lux_auto_ban(self) -> None:
        if getattr(self, "demo", False):
            return
        self.lux_auto_ban_enabled = not self.lux_auto_ban_enabled
        self._reset_lux_auto_ban_schedule()
        self.storage.set_setting(
            "auto_ban_enabled", "1" if self.lux_auto_ban_enabled else "0"
        )
        self._lux_auto_ban_status = (
            "내 밴 차례 대기" if self.lux_auto_ban_enabled else "사용 안 함"
        )
        self._audit_lux_auto_ban(
            "toggle", enabled=self.lux_auto_ban_enabled,
        )
        self._lux_auto_ban_watcher_wake.set()
        self._render_automation_toggles()

    def _reset_lux_auto_ban_schedule(self) -> None:
        lock = getattr(self, "_lux_auto_ban_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._lux_auto_ban_lock = lock
        with lock:
            self._lux_auto_ban_generation = (
                int(getattr(self, "_lux_auto_ban_generation", 0)) + 1
            )
            self._lux_auto_ban_monitoring = False
            self._lux_auto_ban_completed_action_id = None
            self._lux_auto_ban_retry_after = 0.0
            self._lux_auto_ban_action_id = None
            self._lux_auto_ban_target_remaining_ms = 0
            self._lux_auto_ban_fallback_deadline = 0.0
            self._lux_auto_ban_last_remaining_ms = None
            self._lux_auto_ban_last_sampled_at = 0.0
            self._lux_auto_ban_staged = False
            self._lux_auto_ban_display_signature = None

    def _defer_lux_auto_ban_retry(self, seconds: float) -> None:
        with self._lux_auto_ban_lock:
            self._lux_auto_ban_retry_after = max(
                float(getattr(self, "_lux_auto_ban_retry_after", 0.0)),
                time.monotonic() + max(0.0, float(seconds)),
            )

    def _lux_auto_ban_monitor_is_current(
        self, generation: int, action_id: int,
    ) -> bool:
        with self._lux_auto_ban_lock:
            return bool(
                self.lux_auto_ban_enabled
                and self._lux_auto_ban_monitoring
                and self._lux_auto_ban_generation == generation
                and self._lux_auto_ban_action_id == action_id
            )

    def _start_lux_auto_ban_watcher(self) -> None:
        """Keep ban-action discovery alive even if Tk cannot schedule polls."""
        with self._lux_auto_ban_lock:
            if self._lux_auto_ban_watcher_running:
                return
            self._lux_auto_ban_watcher_running = True
        self._audit_lux_auto_ban(
            "watcher_started", enabled=self.lux_auto_ban_enabled,
        )
        threading.Thread(
            target=self._run_lux_auto_ban_watcher,
            name="lux-auto-ban-discovery",
            daemon=True,
        ).start()

    def _run_lux_auto_ban_watcher(self) -> None:
        """Discover my BAN_PICK action without depending on Tk's event queue."""
        wake = self._lux_auto_ban_watcher_wake
        shutdown = getattr(self, "_shutdown_event", None)

        def stopping() -> bool:
            return bool(shutdown is not None and shutdown.is_set())

        last_waiting_key = ""
        missing_session_count = 0

        def publish_waiting(key: str, status: str) -> None:
            nonlocal last_waiting_key
            if key == last_waiting_key:
                return
            last_waiting_key = key
            with self._lux_auto_ban_lock:
                if self._lux_auto_ban_monitoring:
                    return
                generation = self._lux_auto_ban_generation
            self._post_lux_auto_ban_status(generation, status)

        while not stopping():
            auto_accept_on = bool(
                getattr(self, "auto_accept_enabled", False)
                and not getattr(self, "demo", False)
            )
            auto_ban_on = bool(
                getattr(self, "lux_auto_ban_enabled", False)
                and not getattr(self, "demo", False)
            )
            if not auto_accept_on and not auto_ban_on:
                last_waiting_key = ""
                wake.wait()
                wake.clear()
                if stopping():
                    break
                continue
            interval = LUX_AUTO_BAN_IDLE_INTERVAL_SECONDS
            try:
                if auto_accept_on:
                    gameflow_phase = str(
                        self.lcu.get("/lol-gameflow/v1/gameflow-phase")
                    )
                    if gameflow_phase == "ReadyCheck":
                        self._ensure_auto_accept_monitor()
                        interval = min(
                            interval, LUX_AUTO_BAN_DISCOVERY_INTERVAL_SECONDS,
                        )
                    else:
                        self._reset_auto_accept_cycle()
                if not auto_ban_on:
                    missing_session_count = 0
                    wake.wait(interval)
                    wake.clear()
                    if stopping():
                        break
                    continue
                champion_key, _champion_id, champion_name = (
                    self._auto_ban_champion()
                )
                session = self.lcu.champ_select_session()
                missing_session_count = 0
                interval = LUX_AUTO_BAN_DISCOVERY_INTERVAL_SECONDS
                inner_phase = champ_select_timer_phase(session)
                if champion_key in session_banned_champion_ids(session):
                    publish_waiting("BANNED", f"{champion_name} 이미 밴됨")
                elif inner_phase != "BAN_PICK":
                    publish_waiting(
                        f"WAIT:{inner_phase}",
                        (
                            "픽 의사 표시 중 · 실제 밴 단계 대기"
                            if inner_phase in {"PLANNING", "DECLARE"}
                            else "실제 밴 단계 진입 대기"
                        ),
                    )
                else:
                    try:
                        action = find_local_champion_action(
                            session, "ban", require_in_progress=True,
                        )
                    except LcuUnavailable:
                        publish_waiting("BAN_WAIT", "내 밴 차례 대기")
                    else:
                        last_waiting_key = "ACTION"
                        self._ensure_lux_auto_ban_monitor(
                            int(action.get("id") or 0), session,
                        )
            except LcuUnavailable:
                missing_session_count += 1
                if auto_ban_on and missing_session_count >= 3:
                    with self._lux_auto_ban_lock:
                        if not self._lux_auto_ban_monitoring:
                            self._lux_auto_ban_completed_action_id = None
                    publish_waiting("NO_SESSION", "롤 픽·밴 화면 진입 대기")
                interval = LUX_AUTO_BAN_IDLE_INTERVAL_SECONDS
            except Exception as exc:
                # Client startup/phase transitions and malformed transient
                # snapshots are retried; the watcher itself must never die.
                self._audit_lux_auto_ban(
                    "watcher_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                missing_session_count += 1
                if auto_ban_on and missing_session_count >= 3:
                    with self._lux_auto_ban_lock:
                        if not self._lux_auto_ban_monitoring:
                            self._lux_auto_ban_completed_action_id = None
                    publish_waiting("NO_SESSION", "롤 픽·밴 화면 진입 대기")
                interval = LUX_AUTO_BAN_IDLE_INTERVAL_SECONDS
            wake.wait(interval)
            wake.clear()
        with self._lux_auto_ban_lock:
            self._lux_auto_ban_watcher_running = False

    def _post_lux_auto_ban_status(
        self,
        generation: int,
        status: str,
        *,
        remaining_ms: int | None = None,
        completed: bool = False,
    ) -> None:
        """Send monitor state to Tk without making execution depend on Tk."""
        with self._lux_auto_ban_lock:
            if generation != self._lux_auto_ban_generation:
                return
            self._lux_auto_ban_status = status

        def apply() -> None:
            with self._lux_auto_ban_lock:
                if (
                    generation != self._lux_auto_ban_generation
                    or self._lux_auto_ban_status != status
                ):
                    return
            self._render_automation_toggles()
            if completed:
                _key, _champion_id, champion_name = self._auto_ban_champion()
                self.champion_action_status.configure(
                    text=(
                        f"{champion_name} 자동 밴 완료 · 백그라운드에서 밴 단계·"
                        "내 순서·밴 가능 여부를 재확인했습니다."
                    ),
                    fg=COLORS["green"],
                )

        self._post_ui(apply)

    def _record_lux_auto_ban_timer_sample(
        self,
        generation: int,
        action_id: int,
        remaining_ms: int | None,
        sampled_at: float,
    ) -> None:
        """Publish a real LCU timer sample without waiting for Tk."""
        if remaining_ms is None:
            return
        with self._lux_auto_ban_lock:
            if (
                generation != self._lux_auto_ban_generation
                or action_id != self._lux_auto_ban_action_id
                or sampled_at < self._lux_auto_ban_last_sampled_at
            ):
                return
            self._lux_auto_ban_last_remaining_ms = max(0, int(remaining_ms))
            self._lux_auto_ban_last_sampled_at = sampled_at

    def _finish_lux_auto_ban_monitor(
        self, generation: int, action_id: int, *, completed: bool = False,
    ) -> bool:
        """Release only the monitor that still owns this action."""
        with self._lux_auto_ban_lock:
            if (
                generation != self._lux_auto_ban_generation
                or action_id != self._lux_auto_ban_action_id
            ):
                return False
            self._lux_auto_ban_monitoring = False
            self._lux_auto_ban_completed_action_id = (
                action_id if completed else None
            )
            self._lux_auto_ban_action_id = None
            self._lux_auto_ban_target_remaining_ms = 0
            self._lux_auto_ban_fallback_deadline = 0.0
            self._lux_auto_ban_staged = False
            return True

    def _ensure_lux_auto_ban_monitor(
        self, action_id: int, session: dict[str, object],
    ) -> bool:
        """Start one UI-independent monitor for the current local ban action."""
        if action_id <= 0 or not self.lux_auto_ban_enabled:
            return False
        try:
            current_action = find_local_champion_action(
                session, "ban", require_in_progress=True,
            )
        except LcuUnavailable:
            return False
        champion_key, champion_id, champion_name = self._auto_ban_champion()
        current_champion_id = int(current_action.get("championId") or 0)
        if current_champion_id not in {0, champion_key}:
            with self._lux_auto_ban_lock:
                if self._lux_auto_ban_completed_action_id == action_id:
                    return False
                # Cancel a monitor that may have sampled the old empty/Lux
                # value just before this explicit manual choice arrived.
                self._lux_auto_ban_generation += 1
                self._lux_auto_ban_monitoring = False
                self._lux_auto_ban_action_id = None
                self._lux_auto_ban_completed_action_id = action_id
            self._audit_lux_auto_ban(
                "manual_selection_detected",
                action_id=action_id,
                champion_id=current_champion_id,
            )
            return False
        remaining_ms = champ_select_time_left_ms(session)
        now = time.monotonic()
        target_ms = choose_auto_ban_target_ms()
        stage_lead_ms = choose_auto_ban_stage_lead_ms()
        fallback_seconds = random.uniform(
            LUX_AUTO_BAN_FALLBACK_MIN_SECONDS,
            LUX_AUTO_BAN_FALLBACK_MAX_SECONDS,
        )
        deadline = (
            now + max(0.0, (remaining_ms - target_ms) / 1000.0)
            if remaining_ms is not None
            else now + fallback_seconds
        )
        with self._lux_auto_ban_lock:
            if time.monotonic() < getattr(
                self, "_lux_auto_ban_retry_after", 0.0,
            ):
                return False
            if self._lux_auto_ban_completed_action_id == action_id:
                return False
            if (
                self._lux_auto_ban_monitoring
                and self._lux_auto_ban_action_id == action_id
            ):
                return False
            self._lux_auto_ban_generation += 1
            generation = self._lux_auto_ban_generation
            self._lux_auto_ban_monitoring = True
            self._lux_auto_ban_action_id = action_id
            self._lux_auto_ban_target_remaining_ms = target_ms
            self._lux_auto_ban_fallback_deadline = deadline
            self._lux_auto_ban_last_remaining_ms = remaining_ms
            self._lux_auto_ban_last_sampled_at = now
            self._lux_auto_ban_staged = current_champion_id == champion_key

        self._audit_lux_auto_ban(
            "monitor_scheduled",
            generation=generation,
            action_id=action_id,
            remaining_ms=remaining_ms,
            target_ms=target_ms,
            stage_lead_ms=stage_lead_ms,
            champion_key=champion_key,
            champion_id=champion_id,
            already_staged=current_champion_id == champion_key,
        )

        if remaining_ms is None:
            initial_status = (
                f"LCU 타이머 없음 · 감지 후 {fallback_seconds:.1f}초에 안전 검사"
            )
        elif remaining_ms <= target_ms:
            initial_status = (
                f"현재 {remaining_ms / 1000:.1f}초 · 실행 시점 도달 · 안전 검사 중"
            )
        else:
            initial_status = (
                f"현재 {remaining_ms / 1000:.1f}초 · "
                f"{target_ms / 1000:.1f}초 전 밴 예약 · "
                f"확정 약 {stage_lead_ms / 1000:.1f}초 전에 선택 예정"
            )
        self._post_lux_auto_ban_status(
            generation, initial_status, remaining_ms=remaining_ms,
        )
        threading.Thread(
            target=self._run_lux_auto_ban_monitor,
            args=(
                generation, action_id, target_ms, deadline, remaining_ms,
                current_champion_id == champion_key, champion_key,
                stage_lead_ms,
            ),
            name="auto-ban-monitor",
            daemon=True,
        ).start()
        return True

    def _run_lux_auto_ban_monitor(
        self,
        generation: int,
        action_id: int,
        target_ms: int,
        deadline: float,
        initial_remaining_ms: int | None,
        initial_staged: bool = False,
        champion_key: int | None = None,
        stage_lead_ms: int | None = None,
    ) -> None:
        """Guard monitor ownership against every unexpected worker failure."""
        try:
            self._run_lux_auto_ban_monitor_loop(
                generation, action_id, target_ms, deadline,
                initial_remaining_ms, initial_staged, champion_key,
                stage_lead_ms,
            )
        except Exception as exc:
            self._audit_lux_auto_ban(
                "monitor_unexpected_error",
                generation=generation,
                action_id=action_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._defer_lux_auto_ban_retry(0.8)
            if self._finish_lux_auto_ban_monitor(generation, action_id):
                self._post_lux_auto_ban_status(
                    generation,
                    "자동 밴 감시 오류 · 내 밴 차례 다시 감지 중",
                    remaining_ms=initial_remaining_ms,
                )

    def _run_lux_auto_ban_monitor_loop(
        self,
        generation: int,
        action_id: int,
        target_ms: int,
        deadline: float,
        initial_remaining_ms: int | None,
        initial_staged: bool = False,
        champion_key: int | None = None,
        stage_lead_ms: int | None = None,
    ) -> None:
        """Watch Riot's ban timer at 120ms independently of Tk rendering."""
        selected_key = int(champion_key or self._auto_ban_champion()[0])
        _selected_key, champion_id, champion_name = self._auto_ban_champion(
            selected_key
        )
        remaining_ms = initial_remaining_ms
        previous_remaining_ms = initial_remaining_ms
        next_status_at = 0.0
        write_errors = 0
        session_errors = 0
        stage_errors = 0
        staged = initial_staged
        hover_lead_ms = int(
            stage_lead_ms
            if stage_lead_ms is not None
            else choose_auto_ban_stage_lead_ms()
        )
        due_logged = False
        while self._lux_auto_ban_monitor_is_current(generation, action_id):
            now = time.monotonic()
            stage_due = auto_ban_stage_due(
                remaining_ms, target_ms, hover_lead_ms, deadline, now,
            )
            if not staged and stage_due:
                self._audit_lux_auto_ban(
                    "stage_attempt",
                    generation=generation,
                    action_id=action_id,
                    remaining_ms=remaining_ms,
                )
                try:
                    self.lcu.perform_champion_action(
                        selected_key,
                        "ban_hover",
                        expected_action_id=action_id,
                        expected_current_champion_ids={0, selected_key},
                        # The live failure showed this endpoint can reject or
                        # omit a valid ban. The authoritative session action
                        # and Riot's PATCH response remain the hard guards.
                        verify_bannable=False,
                        pre_commit_check=lambda: (
                            self._lux_auto_ban_monitor_is_current(
                                generation, action_id,
                            )
                        ),
                    )
                except LcuActionManualOverride as exc:
                    self._audit_lux_auto_ban(
                        "stage_cancelled", action_id=action_id,
                        remaining_ms=remaining_ms, error=str(exc),
                    )
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id, completed=True,
                    ):
                        self._post_lux_auto_ban_status(
                            generation,
                            f"자동 밴 취소 · {exc}",
                            remaining_ms=remaining_ms,
                        )
                    return
                except LcuActionStateChanged as exc:
                    self._audit_lux_auto_ban(
                        "stage_state_changed", action_id=action_id,
                        remaining_ms=remaining_ms, error=str(exc),
                    )
                    self._defer_lux_auto_ban_retry(0.4)
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id,
                    ):
                        self._post_lux_auto_ban_status(
                            generation,
                            "단계 정보 재확인 중 · 자동 밴 다시 감지",
                            remaining_ms=remaining_ms,
                        )
                    return
                except LcuActionError as exc:
                    self._audit_lux_auto_ban(
                        "stage_action_error", action_id=action_id,
                        remaining_ms=remaining_ms, error=str(exc),
                    )
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id, completed=True,
                    ):
                        self._post_lux_auto_ban_status(
                            generation,
                            f"{champion_name} 선택 실패 · {exc}",
                            remaining_ms=remaining_ms,
                        )
                    return
                except LcuUnavailable as exc:
                    stage_errors += 1
                    self._audit_lux_auto_ban(
                        "stage_transport_error", action_id=action_id,
                        remaining_ms=remaining_ms, attempt=stage_errors,
                        error=str(exc),
                    )
                    if stage_errors >= 3:
                        with self._lux_auto_ban_lock:
                            self._lux_auto_ban_retry_after = max(
                                self._lux_auto_ban_retry_after,
                                time.monotonic() + 1.5,
                            )
                        if self._finish_lux_auto_ban_monitor(
                            generation, action_id,
                        ):
                            self._post_lux_auto_ban_status(
                                generation,
                                f"LCU 연결 실패 · {exc}",
                                remaining_ms=remaining_ms,
                            )
                        return
                    time.sleep(0.25 * stage_errors)
                    continue
                else:
                    staged = True
                    stage_errors = 0
                    with self._lux_auto_ban_lock:
                        if (
                            generation == self._lux_auto_ban_generation
                            and action_id == self._lux_auto_ban_action_id
                        ):
                            self._lux_auto_ban_staged = True
                    self._audit_lux_auto_ban(
                        "stage_success", action_id=action_id,
                        remaining_ms=remaining_ms,
                    )
                    self._post_lux_auto_ban_status(
                        generation,
                        (
                            f"{champion_name} 선택 완료 · {target_ms / 1000:.1f}초에 "
                            "밴 확정"
                        ),
                        remaining_ms=remaining_ms,
                    )

            due = auto_ban_monitor_due(
                remaining_ms, target_ms, deadline, now,
            )
            if due and staged:
                if not due_logged:
                    due_logged = True
                    self._audit_lux_auto_ban(
                        "commit_due", action_id=action_id,
                        remaining_ms=remaining_ms, target_ms=target_ms,
                    )
                self._post_lux_auto_ban_status(
                    generation,
                    f"{champion_name} 밴 실행 검사 중 · 단계·순서·가능 여부 재확인",
                    remaining_ms=remaining_ms,
                )
                try:
                    if not self._lux_auto_ban_monitor_is_current(
                        generation, action_id,
                    ):
                        return
                    self.lcu.perform_champion_action(
                        selected_key,
                        "ban",
                        expected_action_id=action_id,
                        expected_current_champion_ids={selected_key},
                        verify_bannable=False,
                        pre_commit_check=lambda: (
                            self._lux_auto_ban_monitor_is_current(
                                generation, action_id,
                            )
                        ),
                    )
                except LcuActionManualOverride as exc:
                    self._audit_lux_auto_ban(
                        "commit_cancelled", action_id=action_id,
                        remaining_ms=remaining_ms, error=str(exc),
                    )
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id, completed=True,
                    ):
                        self._post_lux_auto_ban_status(
                            generation,
                            "사용자 밴 선택 유지 · 자동 밴 취소",
                            remaining_ms=remaining_ms,
                        )
                    return
                except LcuActionStateChanged as exc:
                    self._audit_lux_auto_ban(
                        "commit_state_changed", action_id=action_id,
                        remaining_ms=remaining_ms, error=str(exc),
                    )
                    self._defer_lux_auto_ban_retry(0.4)
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id,
                    ):
                        self._post_lux_auto_ban_status(
                            generation,
                            "단계 정보 재확인 중 · 자동 밴 다시 감지",
                            remaining_ms=remaining_ms,
                        )
                    return
                except LcuActionError as exc:
                    self._audit_lux_auto_ban(
                        "commit_action_error", action_id=action_id,
                        remaining_ms=remaining_ms, error=str(exc),
                    )
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id, completed=True,
                    ):
                        self._post_lux_auto_ban_status(
                            generation,
                            f"{champion_name} 밴 실패 · {exc}",
                            remaining_ms=remaining_ms,
                        )
                    return
                except LcuUnavailable as exc:
                    write_errors += 1
                    self._audit_lux_auto_ban(
                        "commit_transport_error", action_id=action_id,
                        remaining_ms=remaining_ms, attempt=write_errors,
                        error=str(exc),
                    )
                    if write_errors >= 3:
                        with self._lux_auto_ban_lock:
                            self._lux_auto_ban_retry_after = max(
                                self._lux_auto_ban_retry_after,
                                time.monotonic() + 1.5,
                            )
                        if self._finish_lux_auto_ban_monitor(
                            generation, action_id,
                        ):
                            self._post_lux_auto_ban_status(
                                generation,
                                "LCU 응답 지연 · 자동 밴 재감지 대기",
                                remaining_ms=remaining_ms,
                            )
                        return
                else:
                    self._audit_lux_auto_ban(
                        "commit_success", action_id=action_id,
                        remaining_ms=remaining_ms,
                    )
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id, completed=True,
                    ):
                        self._post_lux_auto_ban_status(
                            generation,
                            f"{champion_name} 자동 밴 완료",
                            remaining_ms=remaining_ms,
                            completed=True,
                        )
                    return

            if now >= next_status_at and not due:
                if not staged:
                    status = (
                        f"{champion_name} 밴 대기 · 현재 {remaining_ms / 1000:.1f}초 · "
                        f"확정 약 {hover_lead_ms / 1000:.1f}초 전에 선택"
                        if remaining_ms is not None else (
                            f"{champion_name} 밴 대기 · LCU 타이머 없음 · "
                            "로컬 예약 시각 감시 중"
                        )
                    )
                else:
                    status = (
                        f"{champion_name} 선택됨 · 현재 {remaining_ms / 1000:.1f}초 · "
                        f"{target_ms / 1000:.1f}초 전 밴 확정 예약"
                        if remaining_ms is not None else (
                            f"{champion_name} 선택됨 · LCU 타이머 없음 · "
                            "로컬 예약 시각 감시 중"
                        )
                    )
                self._post_lux_auto_ban_status(
                    generation, status, remaining_ms=remaining_ms,
                )
                next_status_at = now + LUX_AUTO_BAN_STATUS_INTERVAL_SECONDS

            time.sleep(LUX_AUTO_BAN_MONITOR_INTERVAL_SECONDS)
            if not self._lux_auto_ban_monitor_is_current(
                generation, action_id,
            ):
                return
            try:
                session = self.lcu.champ_select_session()
                if champ_select_timer_phase(session) != "BAN_PICK":
                    raise LcuActionStateChanged("실제 밴 단계가 종료되었습니다.")
                if selected_key in session_banned_champion_ids(session):
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id, completed=True,
                    ):
                        self._post_lux_auto_ban_status(
                            generation, f"{champion_name} 이미 밴됨",
                        )
                    return
                fresh_action = find_local_champion_action(
                    session, "ban", require_in_progress=True,
                )
                if int(fresh_action.get("id") or 0) != action_id:
                    raise LcuActionStateChanged(
                        "내 밴 작업이 변경되었습니다."
                    )
                fresh_champion_id = int(
                    fresh_action.get("championId") or 0
                )
                if fresh_champion_id not in {0, selected_key}:
                    raise LcuActionManualOverride(
                        "사용자가 다른 밴 챔피언을 선택했습니다."
                    )
                staged = staged or fresh_champion_id == selected_key
                if staged:
                    with self._lux_auto_ban_lock:
                        if (
                            generation == self._lux_auto_ban_generation
                            and action_id == self._lux_auto_ban_action_id
                        ):
                            self._lux_auto_ban_staged = True
                fresh_remaining_ms = champ_select_time_left_ms(session)
                now = time.monotonic()
                adjusted_deadline = auto_ban_deadline_after_timer_sample(
                    previous_remaining_ms,
                    fresh_remaining_ms,
                    target_ms,
                    now,
                    deadline,
                )
                if adjusted_deadline != deadline:
                    # Riot explicitly extended/reset the same action timer.
                    # The same rule also replaces the short fallback once a
                    # previously absent Riot timer becomes available.
                    deadline = adjusted_deadline
                    with self._lux_auto_ban_lock:
                        if generation == self._lux_auto_ban_generation:
                            self._lux_auto_ban_fallback_deadline = deadline
                remaining_ms = fresh_remaining_ms
                if fresh_remaining_ms is not None:
                    previous_remaining_ms = fresh_remaining_ms
                    self._record_lux_auto_ban_timer_sample(
                        generation, action_id, fresh_remaining_ms, now,
                    )
                session_errors = 0
            except LcuActionManualOverride as exc:
                self._audit_lux_auto_ban(
                    "manual_override", action_id=action_id,
                    remaining_ms=remaining_ms, error=str(exc),
                )
                if self._finish_lux_auto_ban_monitor(
                    generation, action_id, completed=True,
                ):
                    self._post_lux_auto_ban_status(
                        generation,
                        "사용자 밴 선택 유지 · 자동 밴 취소",
                        remaining_ms=remaining_ms,
                    )
                return
            except LcuActionStateChanged as exc:
                self._audit_lux_auto_ban(
                    "session_state_changed", action_id=action_id,
                    remaining_ms=remaining_ms, error=str(exc),
                )
                self._defer_lux_auto_ban_retry(0.4)
                if self._finish_lux_auto_ban_monitor(
                    generation, action_id,
                ):
                    self._post_lux_auto_ban_status(
                        generation,
                        "단계 전환 감지 · 내 밴 차례 다시 대기",
                        remaining_ms=remaining_ms,
                    )
                return
            except LcuUnavailable:
                session_errors += 1
                if session_errors >= 3:
                    self._defer_lux_auto_ban_retry(1.5)
                    if self._finish_lux_auto_ban_monitor(
                        generation, action_id,
                    ):
                        self._post_lux_auto_ban_status(
                            generation,
                            "LCU 연결 재시도 중 · 자동 밴 재감지 대기",
                            remaining_ms=remaining_ms,
                        )
                    return

    def _show_pick_order_change_notice(
        self, previous_order: int, current_order: int,
    ) -> None:
        self._pick_order_change_notice = (
            f"픽 순서 교환 반영 · {previous_order}픽 → {current_order}픽"
        )
        if self._pick_order_notice_after_id:
            try:
                self.root.after_cancel(self._pick_order_notice_after_id)
            except tk.TclError:
                pass

        def clear_notice() -> None:
            self._pick_order_notice_after_id = None
            self._pick_order_change_notice = ""
            self._selection_panel_signatures.pop("draft_header", None)
            if self._current_main_tab_index() == 0:
                self._render_draft()

        self._pick_order_notice_after_id = self.root.after(5000, clear_notice)

    def _render_automation_toggles(self) -> None:
        auto_on = bool(self.auto_accept_enabled and not self.demo)
        lux_on = bool(self.lux_auto_ban_enabled and not self.demo)
        _champion_key, champion_id, champion_name_ko = self._auto_ban_champion()
        champion_name = self._champion_text(champion_id, champion_name_ko)
        self.auto_accept_button.configure(
            text=self._text(
                "automation.accept.button", state="ON" if auto_on else "OFF",
            ),
            state="disabled" if self.demo else "normal",
        )
        self.lux_auto_ban_button.configure(
            text=self._text(
                "automation.ban.button", champion=champion_name,
                state="ON" if lux_on else "OFF",
            ),
            state="disabled" if self.demo else "normal",
        )
        self._set_button_selected(self.auto_accept_button, auto_on, COLORS["green"])
        self._set_button_selected(self.lux_auto_ban_button, lux_on, COLORS["red"])
        self._render_automation_status_label()

    def _lux_auto_ban_display_text(self) -> str:
        now = time.monotonic()
        _champion_key, champion_id, champion_name_ko = self._auto_ban_champion()
        champion_name = self._champion_text(champion_id, champion_name_ko)
        with self._lux_auto_ban_lock:
            status = self._lux_auto_ban_status
            monitoring = self._lux_auto_ban_monitoring
            staged = bool(getattr(self, "_lux_auto_ban_staged", False))
            remaining_ms = self._lux_auto_ban_last_remaining_ms
            sampled_at = self._lux_auto_ban_last_sampled_at
            target_ms = self._lux_auto_ban_target_remaining_ms
            fallback_deadline = self._lux_auto_ban_fallback_deadline
        if not monitoring:
            return self._tr(status)
        projected = projected_auto_ban_remaining_ms(
            remaining_ms, sampled_at, now,
        )
        if projected is None:
            until_check = max(0.0, fallback_deadline - now)
            return self._text(
                "automation.ban.fallback", champion=champion_name,
                stage=self._text(
                    "automation.ban.staged" if staged else "automation.ban.ready"
                ),
                seconds=until_check,
            )
        remaining_seconds = projected / 1000.0
        until_commit = max(0, projected - max(0, target_ms)) / 1000.0
        if until_commit <= 0:
            return self._text(
                "automation.ban.committing", champion=champion_name,
                stage=self._text(
                    "automation.ban.staged" if staged else "automation.ban.ready"
                ),
                remaining=remaining_seconds,
            )
        return self._text(
            "automation.ban.countdown", champion=champion_name,
            stage=self._text(
                "automation.ban.staged" if staged else "automation.ban.ready"
            ),
            remaining=remaining_seconds, commit=until_commit,
        )

    def _auto_accept_display_text(self) -> str:
        with self._auto_accept_lock:
            status = self._auto_accept_status
            monitoring = self._auto_accept_monitoring
            deadline = self._auto_accept_deadline
        if not monitoring:
            return self._tr(status)
        remaining = max(0.0, deadline - time.monotonic())
        return self._text("automation.accept.countdown", seconds=remaining)

    def _render_automation_status_label(self) -> None:
        auto_on = bool(self.auto_accept_enabled and not self.demo)
        lux_on = bool(self.lux_auto_ban_enabled and not self.demo)
        enabled_status = []
        if auto_on:
            enabled_status.append(self._text(
                "automation.accept.status", status=self._auto_accept_display_text(),
            ))
        if lux_on:
            enabled_status.append(self._text(
                "automation.ban.status", status=self._lux_auto_ban_display_text(),
            ))
        text = " · ".join(enabled_status) if enabled_status else self._text(
            "automation.all_off"
        )
        color = COLORS["green"] if enabled_status else COLORS["muted"]
        signature = (text, color)
        if signature == getattr(self, "_lux_auto_ban_display_signature", None):
            return
        self._lux_auto_ban_display_signature = signature
        self.automation_status_label.configure(text=text, fg=color)

    def _tick_lux_auto_ban_display(self) -> None:
        """Animate only the small status label; never poll LCU from Tk."""
        try:
            if (
                (self.auto_accept_enabled or self.lux_auto_ban_enabled)
                and not self.demo
            ):
                self._render_automation_status_label()
            self.root.after(
                LUX_AUTO_BAN_DISPLAY_INTERVAL_MS,
                self._tick_lux_auto_ban_display,
            )
        except tk.TclError:
            return

    def _reload_data_preferences(self) -> None:
        self._data_preferences = {
            key: self.storage.get_int_setting(key, default, minimum, maximum)
            for key, (default, minimum, maximum) in DATA_PREFERENCE_LIMITS.items()
        }

    def _data_preference(self, key: str) -> int:
        default, _minimum, _maximum = DATA_PREFERENCE_LIMITS[key]
        return int(getattr(self, "_data_preferences", {}).get(key, default))

    def _request_max_age(self, key: str) -> timedelta:
        return timedelta(hours=self._data_preference(key))

    def _meta_snapshot_fresh(self, snapshot: OpggSnapshot) -> bool:
        return lane_matchup_snapshot_fresh(
            snapshot, hours=self._data_preference("opgg_meta_cooldown_hours")
        )

    def _matchup_snapshot_fresh(self, snapshot: OpggSnapshot) -> bool:
        return lane_matchup_snapshot_fresh(
            snapshot, hours=self._data_preference("opgg_matchup_cooldown_hours")
        )

    def _synergy_snapshot_fresh(self, snapshot: OpggSynergySnapshot) -> bool:
        return opgg_synergy_snapshot_fresh(
            snapshot, hours=self._data_preference("opgg_synergy_cooldown_hours")
        )

    @staticmethod
    def _cache_job_preference_key(job_key: str) -> str:
        return {
            "champion_assets": "local_assets_cooldown_hours",
            "rune_catalog": "local_assets_cooldown_hours",
            "opgg_meta_all": "opgg_meta_cooldown_hours",
            "opgg_matchups_all": "opgg_matchup_cooldown_hours",
            "opgg_builds_all": "opgg_build_cooldown_hours",
        }.get(job_key, "local_assets_cooldown_hours")

    def _cache_job_cooldown_remaining(
        self, job_key: str, now: datetime | None = None,
    ) -> timedelta:
        if job_key == "riot_history":
            return self.storage.cache_job_cooldown_remaining(
                job_key, now, hours=1 / 60,
            )
        preference_key = self._cache_job_preference_key(job_key)
        return self.storage.cache_job_cooldown_remaining(
            job_key, now, hours=self._data_preference(preference_key)
        )

    def _build_cooldown_remaining(
        self,
        champion_id: str,
        position: str,
        now: datetime | None = None,
    ) -> timedelta:
        return self.storage.build_guide_cooldown_remaining(
            champion_id, position, now,
            hours=self._data_preference("opgg_build_cooldown_hours"),
        )

    @staticmethod
    def _build_statistics_upgrade_key(
        champion_id: str, position: str,
    ) -> str:
        return f"{str(position).upper()}:{champion_id}"

    def _build_statistics_attempt_map(self) -> dict[str, str]:
        cached = getattr(self, "_build_statistics_attempts", None)
        if isinstance(cached, dict):
            return cached
        raw = self.storage.get_setting(
            f"build_statistics_v{BUILD_STATISTICS_SCHEMA_VERSION}_attempts",
            "{}",
        )
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
        attempts = {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(payload, dict) and str(key) and str(value)
        } if isinstance(payload, dict) else {}
        self._build_statistics_attempts = attempts
        return attempts

    def _build_statistics_upgrade_remaining(
        self,
        champion_id: str,
        position: str,
        now: datetime | None = None,
    ) -> timedelta:
        value = self._build_statistics_attempt_map().get(
            self._build_statistics_upgrade_key(champion_id, position), ""
        )
        try:
            attempted_at = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return timedelta(0)
        current = now or datetime.now(attempted_at.tzinfo)
        if current.tzinfo is None and attempted_at.tzinfo is not None:
            current = current.replace(tzinfo=attempted_at.tzinfo)
        elif current.tzinfo is not None and attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(tzinfo=current.tzinfo)
        if current < attempted_at:
            return self._request_max_age("opgg_build_cooldown_hours")
        return max(
            attempted_at + self._request_max_age("opgg_build_cooldown_hours")
            - current,
            timedelta(0),
        )

    def _mark_build_statistics_upgrade_attempt(
        self, champion_id: str, position: str,
    ) -> None:
        attempts = self._build_statistics_attempt_map()
        attempts[self._build_statistics_upgrade_key(champion_id, position)] = (
            datetime.now().isoformat(timespec="seconds")
        )
        self.storage.set_setting(
            f"build_statistics_v{BUILD_STATISTICS_SCHEMA_VERSION}_attempts",
            json.dumps(attempts, ensure_ascii=False, sort_keys=True),
        )

    def _riot_history_cooldown_remaining(
        self, now: datetime | None = None,
    ) -> timedelta:
        return self.storage.riot_sync_cooldown_remaining(
            now, minutes=1,
        )

    def _cache_manager_position(self) -> str:
        current = str(
            getattr(self, "_cache_manager_active_position", "") or
            self.draft.my_role or "SUPPORT"
        ).upper()
        return "SUPPORT" if current == "UTILITY" else current

    def _cache_job_specs(self) -> list[tuple[str, str, str, str]]:
        return [
            (
                "champion_assets", "챔피언 목록 · 전체 아이콘",
                "현재 패치 한글 챔피언 정보와 모든 챔피언 아이콘을 로컬 저장",
                "챔피언/아이콘 갱신",
            ),
            (
                "rune_catalog", "룬 선택 데이터",
                "롤 클라이언트의 한글 룬 이름·설명·계열을 로컬 저장",
                "룬 데이터 갱신",
            ),
            (
                "opgg_meta_all", "OP.GG 5포지션 메타",
                "TOP·JGL·MID·ADC·SUP 순위와 승률·픽률·밴률 저장",
                "메타 전체 갱신",
            ),
            (
                "opgg_matchups_all", "OP.GG 5포지션 전체 상성표",
                "TOP·JGL·MID·ADC·SUP의 모든 포지션 챔피언 상성표 · 오늘 받은 항목은 건너뜀",
                "상성 전체 갱신",
            ),
            (
                "opgg_builds_all", "전체 챔피언 빌드 · 이미지",
                "5포지션 룬·스펠·아이템 빌드와 사용 이미지를 로컬 저장",
                "전체 빌드 갱신",
            ),
            (
                "riot_history", "내 솔로랭크 전적 1000경기",
                "Riot API로 새 경기만 받아 로컬 전적 DB에 합치고 다시 계산",
                "내 전적 갱신",
            ),
        ]

    @staticmethod
    def _cache_remaining_text(remaining: timedelta) -> str:
        seconds = max(0, int(remaining.total_seconds()))
        if not seconds:
            return "갱신 가능"
        hours, remainder = divmod(seconds, 3600)
        minutes = (remainder + 59) // 60
        if minutes == 60:
            hours += 1
            minutes = 0
        return f"로컬 준비됨 · 다시 요청 {hours:02d}시간 {minutes:02d}분 후"

    def _open_cache_manager(self) -> None:
        window = self._cache_manager_window
        if window and window.winfo_exists():
            window.deiconify()
            window.lift()
            window.focus_force()
            self._refresh_cache_manager_rows()
            self._refresh_cache_manager_champion_button_states()
            return
        window = tk.Toplevel(self.root)
        self._cache_manager_window = window
        window.title("로컬 데이터 캐시 관리")
        window.geometry("960x800")
        window.minsize(780, 680)
        window.configure(bg=COLORS["bg"])
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)

        header = tk.Frame(window, bg=COLORS["bg"], padx=22, pady=18)
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        heading = tk.Frame(header, bg=COLORS["bg"])
        heading.grid(row=0, column=0, sticky="ew")
        tk.Label(
            heading, text="로컬 데이터 미리 받기", bg=COLORS["bg"],
            fg=COLORS["gold"], font=("Malgun Gothic", 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            heading,
            text=(
                "있는 파일과 경기는 재다운로드하지 않습니다. 오래된 캐시도 먼저 보여 주고, "
                "오래된 값은 즉시 표시하고, 같은 외부 요청은 Riot 설정의 주기만큼 막습니다."
            ),
            bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9), wraplength=670, justify="left",
        ).pack(anchor="w", pady=(6, 0))

        current_position = str(self.draft.my_role or "SUPPORT").upper()
        if current_position == "UTILITY":
            current_position = "SUPPORT"
        if current_position not in {position for position, _ in CACHE_POSITION_CHOICES}:
            current_position = "SUPPORT"
        self._cache_manager_active_position = current_position

        notebook = ttk.Notebook(window, style="Selection.TNotebook")
        notebook.pack(fill="both", expand=True, padx=22)
        self._cache_manager_notebook = notebook
        overview_tab = tk.Frame(notebook, bg=COLORS["bg"])
        champions_tab = tk.Frame(notebook, bg=COLORS["bg"])
        notebook.add(overview_tab, text="전체 작업")
        notebook.add(champions_tab, text="챔피언별")

        overview_canvas, overview_content = self._cache_dialog_scroll_area(overview_tab)
        self._cache_manager_overview_canvas = overview_canvas
        self._cache_manager_overview_content = overview_content

        search_bar = tk.Frame(champions_tab, bg=COLORS["panel"], padx=12, pady=9)
        search_bar.pack(fill="x", pady=(8, 6))
        tk.Label(
            search_bar, text="챔피언 검색", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        self._cache_manager_search_var = tk.StringVar()
        search_entry = tk.Entry(
            search_bar, textvariable=self._cache_manager_search_var,
            bg=COLORS["surface"], fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat", font=("Malgun Gothic", 10), width=24,
        )
        search_entry.pack(side="left", padx=(9, 12), ipady=5)
        search_entry.bind("<KeyRelease>", self._schedule_render_cache_champions)
        self._cache_manager_count_label = tk.Label(
            search_bar, text="", bg=COLORS["panel"], fg=COLORS["blue"],
            font=("Malgun Gothic", 8, "bold"),
        )
        self._cache_manager_count_label.pack(side="left")
        tk.Label(
            search_bar, text="로컬 데이터는 즉시 사용 · 없는/오래된 항목만 요청",
            bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        ).pack(side="right")
        position_notebook = ttk.Notebook(
            champions_tab, style="Selection.TNotebook"
        )
        position_notebook.pack(fill="both", expand=True)
        self._cache_manager_position_notebook = position_notebook
        self._cache_manager_position_tabs = {}
        self._cache_manager_champion_contents = {}
        self._cache_manager_champion_canvases = {}
        self._cache_manager_champion_widgets = {}
        self._cache_manager_rendered_queries = {}
        position_tab_labels = {
            "TOP": "TOP", "JUNGLE": "JGL", "MIDDLE": "MID",
            "BOTTOM": "ADC", "SUPPORT": "SUP",
        }
        for position, _label in CACHE_POSITION_CHOICES:
            position_tab = tk.Frame(position_notebook, bg=COLORS["bg"])
            position_notebook.add(
                position_tab, text=position_tab_labels[position]
            )
            position_canvas, position_content = self._cache_dialog_scroll_area(
                position_tab
            )
            self._cache_manager_position_tabs[position] = position_tab
            self._cache_manager_champion_canvases[position] = position_canvas
            self._cache_manager_champion_contents[position] = position_content
        selected_position_tab = self._cache_manager_position_tabs.get(
            current_position,
            self._cache_manager_position_tabs["SUPPORT"],
        )
        position_notebook.select(selected_position_tab)
        self._cache_manager_champion_canvas = (
            self._cache_manager_champion_canvases[current_position]
        )
        self._cache_manager_champion_content = (
            self._cache_manager_champion_contents[current_position]
        )
        position_notebook.bind(
            "<<NotebookTabChanged>>", self._on_cache_manager_position_tab_changed
        )
        setattr(window, "_advisor_scroll_canvas", overview_canvas)
        notebook.bind("<<NotebookTabChanged>>", self._on_cache_manager_tab_changed)

        self._cache_manager_message = tk.Label(
            window, text="전체 작업 또는 챔피언별 갱신을 선택하세요.",
            bg=COLORS["bg"], fg=COLORS["muted"], anchor="w",
            font=("Malgun Gothic", 9, "bold"), padx=22, pady=14,
        )
        self._cache_manager_message.pack(fill="x")
        self._render_cache_manager_overview()
        self._render_cache_manager_champions()

    def _cache_dialog_scroll_area(self, parent: tk.Misc) -> tuple[tk.Canvas, tk.Frame]:
        host = tk.Frame(parent, bg=COLORS["bg"])
        host.pack(fill="both", expand=True)
        canvas = tk.Canvas(host, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            host, orient="vertical", command=canvas.yview,
            style="Advisor.Vertical.TScrollbar",
        )
        content = tk.Frame(canvas, bg=COLORS["bg"])
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content.bind(
            "<Configure>", lambda _event, target=canvas:
            target.configure(scrollregion=target.bbox("all"))
        )
        canvas.bind(
            "<Configure>", lambda event, target=canvas, item=content_window:
            target.itemconfigure(item, width=event.width)
        )
        return canvas, content

    def _on_cache_manager_tab_changed(self, _event: tk.Event | None = None) -> None:
        window = self._cache_manager_window
        notebook = self._cache_manager_notebook
        if not window or not notebook or not window.winfo_exists():
            return
        try:
            index = notebook.index(notebook.select())
        except tk.TclError:
            return
        canvas = (
            self._cache_manager_champion_canvas if index == 1
            else self._cache_manager_overview_canvas
        )
        if canvas:
            setattr(window, "_advisor_scroll_canvas", canvas)

    def _on_cache_manager_position_tab_changed(
        self, _event: tk.Event | None = None,
    ) -> None:
        notebook = self._cache_manager_position_notebook
        if not notebook or not notebook.winfo_exists():
            return
        selected = notebook.select()
        position = next(
            (
                role for role, tab in self._cache_manager_position_tabs.items()
                if str(tab) == selected
            ),
            "SUPPORT",
        )
        self._cache_manager_active_position = position
        self._cache_manager_champion_canvas = (
            self._cache_manager_champion_canvases.get(position)
        )
        self._cache_manager_champion_content = (
            self._cache_manager_champion_contents.get(position)
        )
        window = self._cache_manager_window
        outer_notebook = self._cache_manager_notebook
        champion_tab_visible = False
        if outer_notebook and outer_notebook.winfo_exists():
            try:
                champion_tab_visible = outer_notebook.index(
                    outer_notebook.select()
                ) == 1
            except tk.TclError:
                pass
        if (
            window and window.winfo_exists() and champion_tab_visible
            and self._cache_manager_champion_canvas
        ):
            setattr(
                window, "_advisor_scroll_canvas",
                self._cache_manager_champion_canvas,
            )
        query = (
            self._cache_manager_search_var.get().strip().casefold()
            if self._cache_manager_search_var else ""
        )
        if self._cache_manager_rendered_queries.get(position) != query:
            self._render_cache_manager_champions()
        else:
            # 이미 만든 탭은 위젯을 그대로 보존한다. 저장 작업의 완료 콜백이
            # 해당 행을 갱신하므로 탭 이동 때 DB를 다시 읽을 필요가 없다.
            self._refresh_cache_manager_champion_button_states()

    def _render_cache_manager_overview(self) -> None:
        body = self._cache_manager_overview_content
        if not body or not body.winfo_exists():
            return
        self._clear(body)
        self._cache_manager_rows = {}
        for job_key, title, description, button_text in self._cache_job_specs():
            outer = tk.Frame(body, bg=COLORS["border"], padx=1, pady=1)
            outer.pack(fill="x", pady=5, padx=(0, 4))
            row = tk.Frame(outer, bg=COLORS["panel"], padx=14, pady=11)
            row.pack(fill="x")
            text = tk.Frame(row, bg=COLORS["panel"])
            text.pack(side="left", fill="x", expand=True)
            tk.Label(
                text, text=title, bg=COLORS["panel"], fg=COLORS["text"],
                font=("Malgun Gothic", 10, "bold"),
            ).pack(anchor="w")
            tk.Label(
                text, text=description, bg=COLORS["panel"], fg=COLORS["muted"],
                font=("Malgun Gothic", 8), wraplength=610, justify="left",
            ).pack(anchor="w", pady=(2, 3))
            status = tk.Label(
                text, text="", bg=COLORS["panel"], fg=COLORS["blue"],
                font=("Malgun Gothic", 8, "bold"),
            )
            status.pack(anchor="w")
            button = self._button(
                row, button_text,
                lambda key=job_key: self._start_cache_job(key),
                COLORS["blue"], width=17,
            )
            button.pack(side="right", padx=(12, 0))
            self._cache_manager_rows[job_key] = (status, button)
        self._refresh_cache_manager_rows()

    def _schedule_render_cache_champions(
        self, _event: tk.Event | None = None,
    ) -> None:
        if self._cache_manager_render_after_id:
            try:
                self.root.after_cancel(self._cache_manager_render_after_id)
            except tk.TclError:
                pass
        self._cache_manager_render_after_id = self.root.after(
            120, self._render_cache_manager_champions
        )

    @staticmethod
    def _cache_short_remaining(remaining: timedelta) -> str:
        minutes = max(1, int((remaining.total_seconds() + 59) // 60))
        hours, minutes = divmod(minutes, 60)
        return f"{hours}시간 {minutes:02d}분 남음" if hours else f"{minutes}분 남음"

    @staticmethod
    def _cache_updated_remaining(updated_at: str, hours: int = 24) -> timedelta:
        try:
            saved_at = datetime.fromisoformat(str(updated_at or ""))
        except (TypeError, ValueError):
            return timedelta(0)
        now = datetime.now(saved_at.tzinfo)
        return max(saved_at + timedelta(hours=hours) - now, timedelta(0))

    def _configure_cache_manager_champion_card(
        self,
        position: str,
        champion_id: str,
        widgets: dict[str, object],
        *,
        position_ids: set[str],
        meta_entry: OpggCounter | None,
        guide: ChampionBuildGuide | None,
        snapshot: OpggSnapshot | None,
    ) -> None:
        if meta_entry:
            meta_text = (
                f"{meta_entry.position_rank or '-'}위 · 승률 "
                f"{_fmt_rate(meta_entry.overall_win_rate)}"
            )
            meta_color = COLORS["green"]
        elif position_ids and champion_id not in position_ids:
            meta_text = f"{champion_id} · 비주류 포지션"
            meta_color = COLORS["muted"]
        else:
            meta_text = f"{champion_id} · 메타 순위 없음"
            meta_color = COLORS["muted"]
        widgets["meta_label"].configure(text=meta_text, fg=meta_color)
        position_supported = not position_ids or champion_id in position_ids
        widgets["position_supported"] = position_supported

        guide_remaining = (
            self._cache_updated_remaining(
                guide.updated_at,
                self._data_preference("opgg_build_cooldown_hours"),
            )
            if guide else timedelta(0)
        )
        guide_has_stats = build_guide_has_statistics(guide)
        statistics_upgrade_remaining = (
            self._build_statistics_upgrade_remaining(champion_id, position)
            if guide and not guide_has_stats else timedelta(0)
        )
        statistics_retry_blocked = bool(
            statistics_upgrade_remaining.total_seconds() > 0
        )
        guide_fresh = bool(
            guide and guide_has_stats and guide_remaining.total_seconds() > 0
        )
        if guide_fresh:
            build_text = f"빌드  최신 · {self._cache_short_remaining(guide_remaining)}"
            build_color = COLORS["green"]
        elif guide and not guide_has_stats:
            build_text = (
                "빌드  로컬 사용 · 통계 재시도 "
                f"{self._cache_short_remaining(statistics_upgrade_remaining)}"
                if statistics_retry_blocked else
                "빌드  로컬 사용 · 표본 통계 갱신 필요"
            )
            build_color = COLORS["orange"]
        elif guide:
            build_text = f"빌드  오래됨 · 패치 {guide.patch} 로컬 사용"
            build_color = COLORS["orange"]
        else:
            build_text = "빌드  로컬 데이터 없음"
            build_color = COLORS["muted"]
        widgets["build_label"].configure(text=build_text, fg=build_color)

        matchup_fresh = bool(snapshot and self._matchup_snapshot_fresh(snapshot))
        if matchup_fresh and snapshot and snapshot.raw_status == "NO_DATA":
            matchup_remaining = self._cache_updated_remaining(
                snapshot.updated_at,
                self._data_preference("opgg_matchup_cooldown_hours"),
            )
            matchup_text = (
                f"상성  OP.GG 표본 없음 · "
                f"{self._cache_short_remaining(matchup_remaining)}"
            )
            matchup_color = COLORS["muted"]
        elif matchup_fresh:
            matchup_text = (
                f"상성  최신 · 전체 {_fmt_rate(snapshot.target_overall_win_rate)}"
            )
            matchup_color = COLORS["green"]
        elif snapshot:
            matchup_text = f"상성  오래됨 · 패치 {snapshot.patch} 로컬 사용"
            matchup_color = COLORS["orange"]
        else:
            matchup_text = "상성  로컬 데이터 없음"
            matchup_color = COLORS["muted"]
        widgets["matchup_label"].configure(text=matchup_text, fg=matchup_color)
        widgets["build_fresh"] = guide_fresh
        widgets["build_retry_blocked"] = statistics_retry_blocked
        widgets["matchup_fresh"] = matchup_fresh

        icon_label = widgets.get("icon_label")
        if icon_label:
            icon = self.icon_cache.get(champion_id, 38) \
                if self.icon_cache.is_cached(champion_id) else None
            icon_label.configure(
                image=icon or "", text="" if icon else "?",
                width=40 if icon else 4, height=40 if icon else 2,
            )

    def _refresh_cache_manager_champion_button_states(self) -> None:
        running = self._cache_manager_running
        for (position, champion_id), widgets in (
            self._cache_manager_champion_widgets.items()
        ):
            build_key = f"champion:build:{position}:{champion_id}"
            matchup_key = f"champion:matchup:{position}:{champion_id}"
            build_fresh = bool(widgets.get("build_fresh"))
            build_retry_blocked = bool(widgets.get("build_retry_blocked"))
            matchup_fresh = bool(widgets.get("matchup_fresh"))
            position_supported = bool(widgets.get("position_supported", True))
            widgets["build_button"].configure(
                text=(
                    "진행 중" if running == build_key else
                    "포지션 표본 없음" if not position_supported else
                    "통계 재시도 대기" if build_retry_blocked else
                    "캐시 유효" if build_fresh else "빌드 갱신"
                ),
                state=(
                    "disabled" if self.demo or running or build_fresh
                    or build_retry_blocked
                    or not position_supported
                    else "normal"
                ),
            )
            widgets["matchup_button"].configure(
                text=(
                    "진행 중" if running == matchup_key else
                    "포지션 표본 없음" if not position_supported else
                    "캐시 유효" if matchup_fresh else "상성 갱신"
                ),
                state=(
                    "disabled" if self.demo or running or matchup_fresh
                    or not position_supported
                    else "normal"
                ),
            )

    def _refresh_cache_manager_champion_cards(
        self, position: str | None = None, champion_id: str | None = None,
    ) -> None:
        positions = (
            [position] if position else
            list(dict.fromkeys(role for role, _ in CACHE_POSITION_CHOICES))
        )
        refreshed = False
        for role in positions:
            rows = {
                target_id: widgets
                for (target_role, target_id), widgets
                in self._cache_manager_champion_widgets.items()
                if target_role == role and (
                    champion_id is None or target_id == champion_id
                )
            }
            if not rows:
                continue
            catalog = self.storage.load_opgg_position_catalog(role, max_age=None)
            position_ids = set(catalog[1]) if catalog else set()
            meta = self.storage.load_opgg_snapshot(None, role)
            meta_entries = {
                entry.champion_id: entry for entry in (meta.counters if meta else [])
            }
            build_guides = self.storage.load_build_guides_for_position(role)
            matchup_snapshots = self.storage.load_opgg_snapshots_for_position(role)
            for target_id, widgets in rows.items():
                self._configure_cache_manager_champion_card(
                    role, target_id, widgets,
                    position_ids=position_ids,
                    meta_entry=meta_entries.get(target_id),
                    guide=build_guides.get(target_id),
                    snapshot=matchup_snapshots.get(target_id),
                )
                refreshed = True
        if refreshed:
            self._refresh_cache_manager_champion_button_states()
        elif position == self._cache_manager_position():
            self._render_cache_manager_champions()

    def _render_cache_manager_champions(self) -> None:
        self._cache_manager_render_after_id = None
        position = self._cache_manager_position()
        body = self._cache_manager_champion_contents.get(
            position, self._cache_manager_champion_content
        )
        if not body or not body.winfo_exists():
            return
        self._cache_manager_champion_content = body
        self._cache_manager_champion_canvas = (
            self._cache_manager_champion_canvases.get(
                position, self._cache_manager_champion_canvas
            )
        )
        self._clear(body)
        for key in [
            key for key in self._cache_manager_champion_widgets
            if key[0] == position
        ]:
            self._cache_manager_champion_widgets.pop(key, None)
        query = (
            self._cache_manager_search_var.get().strip().casefold()
            if self._cache_manager_search_var else ""
        )
        self._cache_manager_rendered_queries[position] = query
        catalog = self.storage.load_opgg_position_catalog(position, max_age=None)
        position_ids = set(catalog[1]) if catalog else set()
        meta = self.storage.load_opgg_snapshot(None, position)
        meta_entries = {
            entry.champion_id: entry for entry in (meta.counters if meta else [])
        }
        build_guides = self.storage.load_build_guides_for_position(position)
        matchup_snapshots = self.storage.load_opgg_snapshots_for_position(position)
        role_ids = cache_manager_champion_ids(
            position_ids,
            set(meta_entries),
            set(build_guides),
            set(matchup_snapshots),
        )
        champions = sorted(
            (
                item for item in self.registry.by_id.items()
                if item[0] in role_ids
            ),
            key=lambda item: item[1][1],
        )
        if query:
            champions = [
                item for item in champions
                if query in item[0].casefold() or query in item[1][1].casefold()
            ]
        missing_icons = len(self.icon_cache.missing_ids())
        if self._cache_manager_count_label and self._cache_manager_count_label.winfo_exists():
            self._cache_manager_count_label.configure(
                text=(
                    f"{position_name(position)} 기준 · {len(champions)}명 표시 · "
                    f"아이콘 {'전부 로컬' if not missing_icons else f'{missing_icons}개 없음'}"
                ),
                fg=COLORS["green"] if not missing_icons else COLORS["orange"],
            )
        if not champions:
            tk.Label(
                body,
                text=(
                    "검색 결과가 없습니다." if role_ids else
                    f"{position_name(position)} 챔피언 목록이 없습니다.\n"
                    "전체 작업에서 ‘메타 전체 갱신’을 먼저 실행하세요."
                ),
                bg=COLORS["bg"],
                fg=COLORS["muted"], font=("Malgun Gothic", 10, "bold"),
                pady=30,
            ).pack()
            return

        for column in range(3):
            body.columnconfigure(column, weight=1, uniform="cache_champion")
        for index, (champion_id, (_key, champion_name)) in enumerate(champions):
            outer = tk.Frame(body, bg=COLORS["border"], padx=1, pady=1)
            outer.grid(
                row=index // 3, column=index % 3, sticky="nsew",
                padx=(0, 7), pady=(0, 7),
            )
            card = tk.Frame(outer, bg=COLORS["panel"], padx=10, pady=9)
            card.pack(fill="both", expand=True)
            top = tk.Frame(card, bg=COLORS["panel"])
            top.pack(fill="x")
            icon = self.icon_cache.get(champion_id, 38) \
                if self.icon_cache.is_cached(champion_id) else None
            icon_label = tk.Label(
                top, image=icon or "", text="" if icon else "?",
                width=40 if icon else 4, height=40 if icon else 2,
                bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Malgun Gothic", 13, "bold"),
            )
            icon_label.pack(side="left", padx=(0, 8))
            names = tk.Frame(top, bg=COLORS["panel"])
            names.pack(side="left", fill="x", expand=True)
            tk.Label(
                names, text=champion_name, bg=COLORS["panel"], fg=COLORS["text"],
                font=("Malgun Gothic", 10, "bold"), anchor="w",
            ).pack(fill="x")
            meta_label = tk.Label(
                names, text="", bg=COLORS["panel"], fg=COLORS["muted"],
                font=("Malgun Gothic", 7), anchor="w",
            )
            meta_label.pack(fill="x", pady=(2, 0))

            build_label = tk.Label(
                card, text="", bg=COLORS["panel"], fg=COLORS["muted"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            )
            build_label.pack(fill="x", pady=(8, 2))

            matchup_label = tk.Label(
                card, text="", bg=COLORS["panel"], fg=COLORS["muted"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            )
            matchup_label.pack(fill="x", pady=(1, 7))

            actions = tk.Frame(card, bg=COLORS["panel"])
            actions.pack(fill="x")
            build_button = self._button(
                actions, "빌드 갱신",
                lambda selected=champion_id, selected_position=position:
                self._cache_single_champion(
                    selected, "build", selected_position
                ),
                COLORS["purple"], width=9,
            )
            build_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
            matchup_button = self._button(
                actions, "상성 갱신",
                lambda selected=champion_id, selected_position=position:
                self._cache_single_champion(
                    selected, "matchup", selected_position
                ),
                COLORS["blue"], width=9,
            )
            matchup_button.pack(side="left", fill="x", expand=True)
            widgets: dict[str, object] = {
                "icon_label": icon_label,
                "meta_label": meta_label,
                "build_label": build_label,
                "matchup_label": matchup_label,
                "build_button": build_button,
                "matchup_button": matchup_button,
                "build_fresh": False,
                "matchup_fresh": False,
            }
            self._cache_manager_champion_widgets[(position, champion_id)] = widgets
            self._configure_cache_manager_champion_card(
                position, champion_id, widgets,
                position_ids=position_ids,
                meta_entry=meta_entries.get(champion_id),
                guide=build_guides.get(champion_id),
                snapshot=matchup_snapshots.get(champion_id),
            )
        self._refresh_cache_manager_champion_button_states()

    def _cache_single_champion(
        self, champion_id: str, kind: str, position: str | None = None,
    ) -> None:
        if self.demo or self._cache_manager_running:
            return
        position = str(position or self._cache_manager_position()).upper()
        champion_name = self.registry.ko_name(champion_id)
        catalog = self.storage.load_opgg_position_catalog(position, max_age=None)
        if catalog and champion_id not in set(catalog[1]):
            self._set_cache_manager_message(
                f"{champion_name} · {position_name(position)}는 현재 OP.GG "
                "포지션 표본이 없어 갱신 요청을 보내지 않았습니다.",
                COLORS["orange"],
            )
            self._refresh_cache_manager_champion_cards(position, champion_id)
            return
        if kind == "build":
            cached = self.storage.load_build_guide(champion_id, position)
            if (
                cached and (
                    (
                        build_guide_has_statistics(cached)
                        and self._build_cooldown_remaining(
                            champion_id, position
                        ).total_seconds() > 0
                    )
                    or (
                        not build_guide_has_statistics(cached)
                        and self._build_statistics_upgrade_remaining(
                            champion_id, position
                        ).total_seconds() > 0
                    )
                )
            ):
                self._refresh_cache_manager_champion_cards(position, champion_id)
                return
        else:
            cached_snapshot = self.storage.load_opgg_snapshot(champion_id, position)
            if cached_snapshot and self._matchup_snapshot_fresh(cached_snapshot):
                self._refresh_cache_manager_champion_cards(position, champion_id)
                return
        job_key = f"champion:{kind}:{position}:{champion_id}"
        self._cache_manager_running = job_key
        label = "빌드" if kind == "build" else "상성표"
        self._set_cache_manager_message(
            f"{champion_name} · {position_name(position)} {label} 갱신 중 · 기존 로컬 데이터는 계속 사용",
            COLORS["blue"],
        )
        self._refresh_cache_manager_rows()
        self._refresh_cache_manager_champion_button_states()

        if kind == "build":
            cached = self.storage.load_build_guide(champion_id, position)
            if cached and not build_guide_has_statistics(cached):
                self._mark_build_statistics_upgrade_attempt(
                    champion_id, position
                )

            def work_build() -> tuple[ChampionBuildGuide, dict[str, int]]:
                guide = self.opgg_client.refresh_build(champion_id, position)
                self.storage.save_build_guide(guide)
                return guide, self.build_asset_preloader.cache_guide(guide)

            def success_build(result: tuple[ChampionBuildGuide, dict[str, int]]) -> None:
                guide, assets = result
                if (
                    self._build_selected_champion_id == champion_id
                    and self.draft.my_role == position
                ):
                    self.build_guide = guide
                    self._build_rune_index = 0
                    self._build_spell_index = 0
                    self._render_build()
                self._finish_single_champion_cache(
                    job_key, True,
                    f"{champion_name} {position_name(position)} 빌드 저장 완료 · "
                    f"새 이미지 {assets.get('downloaded', 0)}개",
                )

            self._background(
                work_build, success_build,
                lambda exc: self._finish_single_champion_cache(
                    job_key, False, f"{champion_name} 빌드 갱신 실패 · {exc}"
                ),
            )
            return

        def work_matchup() -> OpggSnapshot:
            snapshot = self.opgg_client.refresh_matchup(champion_id, position)
            self.storage.save_opgg_snapshot(snapshot)
            return snapshot

        def success_matchup(snapshot: OpggSnapshot) -> None:
            if (
                self.draft.my_role == position
                and self.draft.selected_enemy_support_id == champion_id
            ):
                self.opgg_snapshot = snapshot
                self._schedule_selection_render()
            self._finish_single_champion_cache(
                job_key, True,
                (
                    f"{champion_name} {position_name(position)} 상성 표본 없음 · "
                    "재요청 쿨타임 동안 상태 저장"
                    if snapshot.raw_status == "NO_DATA" else
                    f"{champion_name} {position_name(position)} 상성표 저장 완료"
                ),
            )

        self._background(
            work_matchup, success_matchup,
            lambda exc: self._finish_single_champion_cache(
                job_key, False, f"{champion_name} 상성표 갱신 실패 · {exc}"
            ),
        )

    def _finish_single_champion_cache(
        self, job_key: str, success: bool, message: str,
    ) -> None:
        if self._cache_manager_running == job_key:
            self._cache_manager_running = ""
        self._set_cache_manager_message(
            message, COLORS["green"] if success else COLORS["orange"]
        )
        self._refresh_cache_manager_rows()
        parts = job_key.split(":", 3)
        if len(parts) == 4:
            _scope, _kind, position, champion_id = parts
            self._refresh_cache_manager_champion_cards(position, champion_id)
        else:
            self._refresh_cache_manager_champion_button_states()

    def _refresh_cache_manager_rows(self) -> None:
        if not self._cache_manager_rows:
            return
        running = self._cache_manager_running
        for job_key, _title, _description, button_text in self._cache_job_specs():
            widgets = self._cache_manager_rows.get(job_key)
            if not widgets:
                continue
            status, button = widgets
            remaining = self._cache_job_cooldown_remaining(job_key)
            if running == job_key:
                status.configure(text="갱신 중 · 기존 로컬 데이터는 계속 사용", fg=COLORS["blue"])
                button.configure(text="진행 중...", state="disabled")
            elif running:
                status.configure(
                    text=self._cache_remaining_text(remaining),
                    fg=COLORS["green"] if remaining.total_seconds() else COLORS["muted"],
                )
                button.configure(text=button_text, state="disabled")
            elif remaining.total_seconds() > 0:
                status.configure(text=self._cache_remaining_text(remaining), fg=COLORS["green"])
                button.configure(text=button_text, state="disabled")
            else:
                status.configure(
                    text="갱신 가능 · 기존 개별 캐시는 먼저 표시하고 필요한 요청만 실행",
                    fg=COLORS["orange"],
                )
                button.configure(text=button_text, state="normal")

    def _set_cache_manager_message(self, text: str, color: str = COLORS["blue"]) -> None:
        label = self._cache_manager_message
        if label and label.winfo_exists():
            label.configure(text=text, fg=color)

    def _finish_cache_job(self, job_key: str, success: bool, message: str) -> None:
        if success:
            self.storage.mark_cache_job_success(job_key)
        self._cache_manager_running = ""
        self._set_cache_manager_message(
            message, COLORS["green"] if success else COLORS["orange"]
        )
        self._refresh_cache_manager_rows()
        self._refresh_cache_manager_champion_cards()
        self._refresh_cache_manager_champion_button_states()

    def _start_cache_job(self, job_key: str) -> None:
        if self.demo or self._cache_manager_running:
            return
        remaining = self._cache_job_cooldown_remaining(job_key)
        if remaining.total_seconds() > 0:
            self._refresh_cache_manager_rows()
            return
        self._cache_manager_running = job_key
        self._refresh_cache_manager_rows()
        self._refresh_cache_manager_champion_button_states()
        if job_key == "champion_assets":
            self._cache_champion_assets(job_key)
        elif job_key == "rune_catalog":
            self._cache_rune_data(job_key)
        elif job_key == "opgg_meta_all":
            self._cache_all_meta(job_key)
        elif job_key == "opgg_matchups_all":
            self._cache_all_position_matchups(job_key)
        elif job_key == "opgg_builds_all":
            self._toggle_bulk_build_download(
                lambda ok: self._finish_cache_job(
                    job_key, ok,
                    "전체 빌드와 룬·스펠·아이템 이미지 저장 완료" if ok
                    else "전체 빌드 저장이 취소되었거나 실패했습니다.",
                )
            )
        elif job_key == "riot_history":
            self._sync_riot(
                on_complete=lambda ok: self._finish_cache_job(
                    job_key, ok,
                    "내 솔로랭크 전적 로컬 저장 완료" if ok
                    else "내 전적 갱신을 완료하지 못했습니다.",
                )
            )

    def _cache_champion_assets(self, job_key: str) -> None:
        def prefetch(count: int) -> None:
            self._refresh_build_champion_values()
            missing = self.icon_cache.missing_ids()
            if not missing:
                self._finish_cache_job(
                    job_key, True,
                    f"챔피언 {count}개와 아이콘이 이미 모두 로컬에 있습니다. 재다운로드하지 않았습니다.",
                )
                return
            self.icon_cache.prefetch_all(
                lambda: self._finish_cache_job(
                    job_key, True,
                    f"챔피언 {count}개 확인 · 빠진 아이콘 {len(missing)}개 저장 완료",
                )
            )

        self._background(
            self.registry.refresh, prefetch,
            lambda exc: self._finish_cache_job(
                job_key, False, f"현재 챔피언 목록 확인 실패 · 기존 로컬 목록은 유지합니다 · {exc}"
            ),
        )

    def _cache_rune_data(self, job_key: str) -> None:
        self._background(
            lambda: self.rune_catalog.refresh_from_lcu(self.lcu),
            lambda count: self._finish_cache_job(
                job_key, True, f"한글 룬 {count}개 로컬 저장 완료"
            ),
            lambda exc: self._finish_cache_job(
                job_key, False, f"롤 클라이언트에서 룬을 읽지 못했습니다 · {exc}"
            ),
        )

    def _cache_all_meta(self, job_key: str) -> None:
        positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")

        def work() -> list[OpggSnapshot]:
            snapshots: list[OpggSnapshot] = []
            for index, position in enumerate(positions, start=1):
                self._post_ui(lambda done=index: self._set_cache_manager_message(
                    f"OP.GG 포지션 메타 저장 중 · {done}/5"
                ))
                snapshot = self.storage.load_opgg_snapshot(None, position)
                if not snapshot or not self._meta_snapshot_fresh(snapshot):
                    snapshot = self.opgg_client.refresh_overall(position)
                    self.storage.save_opgg_snapshot(snapshot)
                snapshots.append(snapshot)
            return snapshots

        def success(snapshots: list[OpggSnapshot]) -> None:
            current = next(
                (item for item in snapshots if item.position == self.draft.my_role), None
            )
            if current:
                self.opgg_meta_snapshot = current
                if not self.draft.selected_enemy_support_id:
                    self.opgg_snapshot = current
                self._schedule_selection_render()
            self._finish_cache_job(job_key, True, "5개 포지션 메타 로컬 저장 완료")

        self._background(
            work, success,
            lambda exc: self._finish_cache_job(job_key, False, f"메타 갱신 실패 · {exc}"),
        )

    def _cache_all_position_matchups(self, job_key: str) -> None:
        positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")

        def work() -> tuple[int, int, int, int]:
            jobs: list[tuple[str, str]] = []
            catalog_failures = 0
            for position in positions:
                catalog = self.storage.load_opgg_position_catalog(
                    position,
                    max_age=self._request_max_age("opgg_matchup_cooldown_hours"),
                )
                if catalog:
                    _patch, champion_ids = catalog
                else:
                    try:
                        _patch, champion_ids = self.opgg_client.refresh_position_champions(
                            position
                        )
                        self.storage.save_opgg_position_catalog(
                            position, _patch, champion_ids
                        )
                    except Exception:
                        catalog_failures += 1
                        continue
                jobs.extend((position, champion_id) for champion_id in champion_ids)
            jobs = list(dict.fromkeys(jobs))
            saved = cached = failed = 0
            total = len(jobs)
            for index, (position, champion_id) in enumerate(jobs, start=1):
                snapshot = self.storage.load_opgg_snapshot(champion_id, position)
                if snapshot and self._matchup_snapshot_fresh(snapshot):
                    cached += 1
                else:
                    try:
                        snapshot = self.opgg_client.refresh_matchup(champion_id, position)
                        self.storage.save_opgg_snapshot(snapshot)
                        saved += 1
                    except Exception:
                        failed += 1
                    time.sleep(0.45)
                self._post_ui(
                    lambda done=index, count=total, role=position,
                           new=saved, old=cached, errors=failed + catalog_failures:
                    self._set_cache_manager_message(
                        f"5포지션 상성표 {done}/{count} · 현재 {position_name(role)} · "
                        f"신규 {new} · 재사용 {old} · 실패 {errors}"
                    )
                )
            return saved, cached, failed, catalog_failures

        def success(result: tuple[int, int, int, int]) -> None:
            saved, cached, failed, catalog_failures = result
            ok = saved + cached > 0
            self._finish_cache_job(
                job_key, ok,
                f"5포지션 전체 상성표 완료 · 신규 {saved} · 재사용 {cached} · "
                f"상성 실패 {failed} · 목록 실패 {catalog_failures}",
            )

        self._background(
            work, success,
            lambda exc: self._finish_cache_job(
                job_key, False, f"5포지션 전체 상성표 갱신 실패 · {exc}"
            ),
        )

    def _build_draft_panel(self) -> None:
        panel = self._panel(
            self.content,
            (
                "1 · 현재 드래프트와 추천 요청"
                if self.codex_recommendations_enabled
                else "1 · 현재 드래프트"
            ),
            COLORS["gold"],
        )
        self.draft_panel_title_label = getattr(
            panel, "_advisor_title_label", None
        )
        self.pick_order_label = tk.Label(
            panel, bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 9)
        )
        self.pick_order_label.pack(anchor="w", pady=(0, 8))

        bans_row = tk.Frame(panel, bg=COLORS["panel"])
        bans_row.pack(fill="x", pady=(0, 10))
        self.ally_bans_frame = self._labeled_chip_row(bans_row, "우리 밴")
        self.enemy_bans_frame = self._labeled_chip_row(bans_row, "상대 밴")

        tk.Label(
            panel, text="우리 팀  ·  ■ 확정 픽   ◇ 픽 의사(HOVER)   ● 내 픽",
            bg=COLORS["panel"],
            fg=COLORS["muted"], font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w")
        self.ally_picks_frame = tk.Frame(panel, bg=COLORS["panel"])
        self.ally_picks_frame.pack(fill="x", pady=(6, 12))

        self.enemy_instruction_label = tk.Label(
            panel, text="상대 팀  ·  적 서포터를 직접 클릭해서 지정", bg=COLORS["panel"],
            fg=COLORS["muted"], font=("Malgun Gothic", 9, "bold"),
        )
        self.enemy_instruction_label.pack(anchor="w")
        self.enemy_picks_frame = tk.Frame(panel, bg=COLORS["panel"])
        self.enemy_picks_frame.pack(fill="x", pady=(6, 8))
        enemy_choice_row = tk.Frame(panel, bg=COLORS["panel"])
        enemy_choice_row.pack(fill="x")
        self.enemy_unknown_button = self._button(
            enemy_choice_row, f"적 {position_name(self.draft.my_role)} 모르겠음",
            self._select_unknown_enemy_support, COLORS["orange"], width=20,
        )
        self.enemy_unknown_button.pack(side="left", padx=(0, 10))
        self.enemy_support_label = tk.Label(
            enemy_choice_row, bg=COLORS["panel"], fg=COLORS["blue"],
            font=("Malgun Gothic", 10, "bold")
        )
        self.enemy_support_label.pack(side="left", fill="x", expand=True)
        self.stale_label = tk.Label(
            panel, text="", bg=COLORS["panel"], fg=COLORS["orange"], font=("Malgun Gothic", 9)
        )
        self.stale_label.pack(anchor="w", pady=(4, 0))

        self.hover_matchup_card = tk.Frame(
            panel, bg=COLORS["surface"], padx=12, pady=10,
            highlightthickness=1, highlightbackground=COLORS["divider"],
        )
        self.hover_matchup_card.pack(fill="x", pady=(10, 0))
        hover_header = tk.Frame(self.hover_matchup_card, bg=COLORS["surface"])
        hover_header.pack(fill="x")
        self.hover_matchup_badge = tk.Label(
            hover_header, text="HOVER 대기", bg=COLORS["chip"], fg=COLORS["muted"],
            padx=8, pady=4, font=("Malgun Gothic", 8, "bold"),
        )
        self.hover_matchup_badge.pack(side="left")
        self.hover_matchup_title = tk.Label(
            hover_header, text="내 픽 의사 즉시 상성", bg=COLORS["surface"],
            fg=COLORS["text"], font=("Malgun Gothic", 10, "bold"), anchor="w",
        )
        self.hover_matchup_title.pack(side="left", padx=(9, 0), fill="x", expand=True)
        self.hover_matchup_cache = tk.Label(
            hover_header, text="로컬 캐시 우선", bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Malgun Gothic", 8, "bold"),
        )
        self.hover_matchup_cache.pack(side="right")

        hover_body = tk.Frame(self.hover_matchup_card, bg=COLORS["surface"])
        hover_body.pack(fill="x", pady=(9, 0))
        ally_icon_box = tk.Frame(
            hover_body, width=46, height=46, bg=COLORS["panel_2"],
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        ally_icon_box.pack(side="left")
        ally_icon_box.pack_propagate(False)
        self.hover_matchup_ally_icon = tk.Label(
            ally_icon_box, text="?", bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 13, "bold"),
        )
        self.hover_matchup_ally_icon.pack(fill="both", expand=True)
        self.hover_matchup_ally_name = tk.Label(
            hover_body, text="내 챔피언 대기", bg=COLORS["surface"], fg=COLORS["muted"],
            width=14, anchor="w", font=("Malgun Gothic", 10, "bold"),
        )
        self.hover_matchup_ally_name.pack(side="left", padx=(9, 4))
        tk.Label(
            hover_body, text="VS", bg=COLORS["surface"], fg=COLORS["gold"],
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=5)
        enemy_icon_box = tk.Frame(
            hover_body, width=46, height=46, bg=COLORS["panel_2"],
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        enemy_icon_box.pack(side="left")
        enemy_icon_box.pack_propagate(False)
        self.hover_matchup_enemy_icon = tk.Label(
            enemy_icon_box, text="?", bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 13, "bold"),
        )
        self.hover_matchup_enemy_icon.pack(fill="both", expand=True)
        self.hover_matchup_enemy_name = tk.Label(
            hover_body, text="상대 대기", bg=COLORS["surface"], fg=COLORS["muted"],
            width=14, anchor="w", font=("Malgun Gothic", 10, "bold"),
        )
        self.hover_matchup_enemy_name.pack(side="left", padx=(9, 13))

        hover_stats = tk.Frame(hover_body, bg=COLORS["panel_2"], padx=11, pady=7)
        hover_stats.pack(side="left", fill="x", expand=True)
        self.hover_matchup_rate = tk.Label(
            hover_stats, text="롤에서 챔피언을 올려놓으면 즉시 비교합니다.",
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 10, "bold"), anchor="w",
        )
        self.hover_matchup_rate.pack(fill="x")
        self.hover_matchup_detail = tk.Label(
            hover_stats, text="OP.GG 상성 · 내 맞상대 전적 · 원딜 조합을 한 번에 확인",
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8), anchor="w",
        )
        self.hover_matchup_detail.pack(fill="x", pady=(3, 0))
        self.hover_matchup_local = tk.Label(
            self.hover_matchup_card, text="", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"), anchor="w",
        )
        self.hover_matchup_local.pack(fill="x", pady=(7, 0))

        workflow = tk.Frame(
            panel, bg=COLORS["surface"], padx=11, pady=9,
            highlightthickness=1, highlightbackground=COLORS["divider"],
        )
        workflow.pack(fill="x", pady=(12, 0))
        self.codex_workflow_frame = workflow
        step_row = tk.Frame(workflow, bg=COLORS["surface"])
        step_row.pack(fill="x")
        self.workflow_steps: list[tuple[tk.Frame, tk.Label, tk.Label]] = []
        for column, (number, title) in enumerate((
            ("1", "규칙 1회 등록"), ("2", "상대 확인"),
            ("3", "CLI 질문"), ("4", "추천 직접 선택"),
        )):
            step = tk.Frame(
                step_row, bg=COLORS["panel_2"], padx=9, pady=6,
                highlightthickness=1, highlightbackground=COLORS["border"],
            )
            step.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 4))
            step_row.grid_columnconfigure(column, weight=1, uniform="workflow_steps")
            badge = tk.Label(
                step, text=number, bg=COLORS["chip"], fg=COLORS["muted"],
                width=2, font=("Malgun Gothic", 8, "bold"),
            )
            badge.pack(side="left", padx=(0, 7))
            label = tk.Label(
                step, text=title, bg=COLORS["panel_2"], fg=COLORS["muted"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            )
            label.pack(side="left", fill="x", expand=True)
            self.workflow_steps.append((step, badge, label))

        cli_row = tk.Frame(workflow, bg=COLORS["surface"])
        cli_row.pack(fill="x", pady=(9, 0))
        self.codex_recommend_button = self._button(
            cli_row, "Codex CLI로 추천 받기", self._request_codex_recommendations,
            COLORS["green"], width=24, filled=True,
        )
        self.codex_recommend_button.pack(side="left", padx=(0, 10))
        self.codex_cli_status_label = tk.Label(
            cli_row, text="", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9, "bold"), anchor="w",
        )
        self.codex_cli_status_label.pack(side="left", fill="x", expand=True)

        action_row = tk.Frame(workflow, bg=COLORS["surface"])
        action_row.pack(fill="x", pady=(7, 0))
        self.memory_prompt_button = self._button(
            action_row, "규칙 복사(수동)", self._copy_memory_prompt, COLORS["gold"],
            width=17,
        )
        self.memory_prompt_button.pack(side="left", padx=(0, 7))
        self.copy_button = self._button(
            action_row, "현재 픽 짧게 복사", self._copy_prompt, COLORS["purple"],
            width=19, filled=True,
        )
        self.copy_button.pack(side="left", padx=(0, 7))
        self.preview_prompt_button = self._button(
            action_row, "짧은 질문 미리보기", self._show_prompt_preview,
            COLORS["blue"], width=18,
        )
        self.preview_prompt_button.pack(side="left", padx=(0, 7))
        self.paste_button = self._button(
            action_row, "답변 붙여넣고 분석", self._paste_clipboard_response,
            COLORS["green"], width=21, filled=True,
        )
        self.paste_button.pack(side="left")
        self.exchange_status = tk.Label(
            action_row, text="", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"), anchor="w",
        )
        self.exchange_status.pack(side="left", padx=12, fill="x", expand=True)

    def _labeled_chip_row(self, parent: tk.Widget, title: str) -> tk.Frame:
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(side="left", fill="x", expand=True)
        tk.Label(row, text=title, bg=COLORS["panel"], fg=COLORS["muted"], width=8,
                 anchor="w", font=("Malgun Gothic", 9, "bold")).pack(side="left")
        chip_frame = tk.Frame(row, bg=COLORS["panel"])
        chip_frame.pack(side="left", fill="x", expand=True)
        return chip_frame

    def _build_selection_detail_tabs(self) -> None:
        section = tk.Frame(self.content, bg=COLORS["bg"])
        section.pack(fill="x", padx=22, pady=(0, 11))
        self.selection_detail_section = section
        heading = tk.Frame(section, bg=COLORS["bg"])
        heading.pack(fill="x", pady=(0, 7))
        self.selection_detail_heading_label = tk.Label(
            heading, text="3 · 상세 분석", bg=COLORS["bg"], fg=COLORS["text"],
            font=("Malgun Gothic", 11, "bold"),
        )
        self.selection_detail_heading_label.pack(side="left")
        tk.Label(
            heading, text="필요한 정보만 탭으로 열어 한 화면에서 비교합니다.",
            bg=COLORS["bg"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        ).pack(side="left", padx=(12, 0))

        host = tk.Frame(section, bg=COLORS["border"], padx=1, pady=1)
        host.pack(fill="x")
        notebook = ttk.Notebook(host, style="Selection.TNotebook")
        notebook.pack(fill="x")
        self.selection_detail_notebook = notebook
        self.selection_synergy_tab = tk.Frame(
            notebook, bg=COLORS["bg"], padx=8, pady=8,
        )
        self.selection_meta_tab = tk.Frame(
            notebook, bg=COLORS["bg"], padx=8, pady=8,
        )
        self.selection_matchup_tab = tk.Frame(
            notebook, bg=COLORS["bg"], padx=8, pady=8,
        )
        self.selection_chat_tab = tk.Frame(
            notebook, bg=COLORS["bg"], padx=8, pady=8,
        )
        notebook.add(self.selection_synergy_tab, text="원딜 × 서포터 조합")
        notebook.add(self.selection_meta_tab, text="OP.GG 포지션 메타")
        notebook.add(self.selection_matchup_tab, text="상대 상성 · 내 전적")
        notebook.add(self.selection_chat_tab, text="ChatGPT 답변 편집")

        self._build_synergy_panel(self.selection_synergy_tab)
        self._build_opgg_meta_panel(self.selection_meta_tab)
        self._build_opgg_panel(self.selection_matchup_tab)
        self._build_manual_panel(self.selection_chat_tab)

        saved_tab = self.storage.get_setting("selection_detail_tab")
        try:
            selected_index = min(max(int(saved_tab or 0), 0), 3)
        except ValueError:
            selected_index = 0
        notebook.select(selected_index)
        notebook.bind(
            "<<NotebookTabChanged>>", self._on_selection_detail_tab_changed,
        )

    def _on_selection_detail_tab_changed(
        self, _event: tk.Event | None = None,
    ) -> None:
        if not hasattr(self, "selection_detail_notebook"):
            return
        try:
            index = self.selection_detail_notebook.index(
                self.selection_detail_notebook.select()
            )
        except tk.TclError:
            return
        self._pending_selection_detail_index = index
        if self._selection_detail_save_after_id:
            try:
                self.root.after_cancel(self._selection_detail_save_after_id)
            except tk.TclError:
                pass

        def save() -> None:
            self._selection_detail_save_after_id = None
            self.storage.set_setting(
                "selection_detail_tab", str(self._pending_selection_detail_index)
            )

        # Do not open/commit SQLite in the same frame as the visual tab switch.
        self._selection_detail_save_after_id = self.root.after(350, save)

    def _build_opgg_meta_panel(self, parent: tk.Widget) -> None:
        panel = self._panel(
            parent, "OP.GG 포지션 메타 순위", COLORS["gold"], outer_padx=0,
            outer_pady=(0, 0),
        )
        self.opgg_meta_summary_label = tk.Label(
            panel, text="캐시된 포지션 순위가 없습니다.", bg=COLORS["panel"],
            fg=COLORS["muted"], justify="left", anchor="w", font=("Malgun Gothic", 9),
        )
        self.opgg_meta_summary_label.pack(fill="x", pady=(0, 9))
        columns = ("rank", "winrate", "pickrate", "banrate", "personal", "status")
        self.opgg_meta_tree = ttk.Treeview(
            panel, columns=columns, show="tree headings",
            height=self._data_preference("opgg_meta_display_count"),
            style="Advisor.Treeview",
        )
        headings = {
            "rank": "OP.GG 순위", "winrate": "승률", "pickrate": "픽률",
            "banrate": "밴률", "personal": "내 챔피언 전적", "status": "현재 선택",
        }
        self.opgg_meta_tree.heading("#0", text="요즘 강한 챔피언")
        self.opgg_meta_tree.column("#0", width=185, anchor="w", stretch=True)
        widths = {
            "rank": 90, "winrate": 90, "pickrate": 90,
            "banrate": 90, "personal": 170, "status": 110,
        }
        for column in columns:
            self.opgg_meta_tree.heading(column, text=headings[column])
            self.opgg_meta_tree.column(
                column, width=widths[column], anchor="center", stretch=True
            )
        self.opgg_meta_tree.pack(fill="x")
        self.opgg_meta_tree.tag_configure("top3", foreground=COLORS["green"])
        self.opgg_meta_tree.tag_configure("top10", foreground=COLORS["gold"])
        self.opgg_meta_tree.tag_configure("blocked", foreground=COLORS["muted"])

    def _build_synergy_panel(self, parent: tk.Widget) -> None:
        panel = self._panel(
            parent, "원딜 × 서포터 조합 추천", COLORS["green"],
            outer_padx=0, outer_pady=(0, 0),
        )
        top = tk.Frame(panel, bg=COLORS["panel"])
        top.pack(fill="x", pady=(0, 9))
        self.synergy_summary_label = tk.Label(
            top, text="아군 원딜 선택 대기", bg=COLORS["panel"],
            fg=COLORS["muted"], justify="left", anchor="w",
            font=("Malgun Gothic", 9),
        )
        self.synergy_summary_label.pack(side="left", fill="x", expand=True)
        self.synergy_flow_label = tk.Label(
            top, text="", bg=COLORS["panel"], fg=COLORS["blue"],
            font=("Malgun Gothic", 8, "bold"), anchor="e",
        )
        self.synergy_flow_label.pack(side="right", padx=(12, 0))
        columns = ("rank", "opgg", "sample", "local", "tier", "status")
        self.synergy_tree = ttk.Treeview(
            panel, columns=columns, show="tree headings", height=6,
            style="Advisor.Treeview",
        )
        headings = {
            "rank": "조합 순위", "opgg": "OP.GG 조합 승률",
            "sample": "OP.GG 표본", "local": "내 로컬 조합",
            "tier": "시너지 티어", "status": "추천 상태",
        }
        self.synergy_tree.heading("#0", text="서포터")
        self.synergy_tree.column("#0", width=180, anchor="w", stretch=True)
        widths = {
            "rank": 85, "opgg": 125, "sample": 105,
            "local": 170, "tier": 90, "status": 110,
        }
        for column in columns:
            self.synergy_tree.heading(column, text=headings[column])
            self.synergy_tree.column(
                column, width=widths[column], anchor="center", stretch=True,
            )
        self.synergy_tree.pack(fill="x")
        self.synergy_tree.tag_configure("best", foreground=COLORS["green"])
        self.synergy_tree.tag_configure("good", foreground=COLORS["gold"])
        self.synergy_tree.tag_configure("blocked", foreground=COLORS["muted"])

    def _build_opgg_panel(self, parent: tk.Widget) -> None:
        panel = self._panel(
            parent, "OP.GG 상대 상성 및 내 전적", COLORS["blue"],
            outer_padx=0, outer_pady=(0, 0),
        )
        self.opgg_summary_label = tk.Label(
            panel, text="캐시된 데이터가 없습니다.", bg=COLORS["panel"], fg=COLORS["muted"],
            justify="left", anchor="w", font=("Malgun Gothic", 9),
        )
        self.opgg_summary_label.pack(fill="x", pady=(0, 9))
        controls = tk.Frame(panel, bg=COLORS["panel"])
        controls.pack(fill="x", pady=(0, 10))
        self.position_filter_label = tk.Label(
            controls, text="플레이 유형", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"),
        )
        self.position_filter_label.pack(side="left", padx=(0, 8))
        self.support_filter_buttons: dict[str, tk.Button] = {}
        for key, label in SUPPORT_FILTER_LABELS.items():
            button = self._button(
                controls, label, lambda selected=key: self._set_support_filter(selected),
                COLORS["blue"], width=9,
            )
            button.configure(padx=7, pady=5, font=("Malgun Gothic", 8, "bold"))
            button.pack(side="left", padx=(0, 5))
            button.bind("<Leave>", lambda _e: self._refresh_filter_buttons())
            self.support_filter_buttons[key] = button
        self.copy_top3_button = self._button(
            controls, "TOP 3 후보 복사", self._copy_top3_candidates, COLORS["gold"]
        )
        self.copy_top3_button.configure(padx=10, pady=5, font=("Malgun Gothic", 8, "bold"))
        self.copy_top3_button.pack(side="right")
        self.opgg_calc_label = tk.Label(
            controls, text="", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        )
        self.opgg_calc_label.pack(side="right", padx=(0, 10))

        view_row = tk.Frame(panel, bg=COLORS["panel"])
        view_row.pack(fill="x", pady=(0, 8))
        tk.Label(
            view_row, text="표시 기준", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="left", padx=(0, 8))
        self.opgg_view_buttons: dict[str, tk.Button] = {}
        for key, label, accent in (
            ("OPGG", "OP.GG 상대 상성", COLORS["blue"]),
            ("PERSONAL", "내 챔피언 전적", COLORS["green"]),
            ("MATCHUP", "내 맞상대 전적", COLORS["purple"]),
        ):
            button = self._button(
                view_row, label,
                lambda selected=key: self._set_opgg_detail_view(selected),
                accent, width=16,
            )
            button.configure(padx=8, pady=5, font=("Malgun Gothic", 8, "bold"))
            button.pack(side="left", padx=(0, 5))
            self.opgg_view_buttons[key] = button
        self.opgg_view_hint = tk.Label(
            view_row, text="", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8), anchor="e",
        )
        self.opgg_view_hint.pack(side="right", fill="x", expand=True)

        table_host = tk.Frame(panel, bg=COLORS["panel"])
        table_host.pack(fill="x")
        self.opgg_table_frames: dict[str, tk.Frame] = {}

        def make_tree(
            key: str, columns: tuple[str, ...], headings: dict[str, str],
            widths: dict[str, int], first_heading: str,
        ) -> ttk.Treeview:
            frame = tk.Frame(table_host, bg=COLORS["panel"])
            self.opgg_table_frames[key] = frame
            tree = ttk.Treeview(
                frame, columns=columns, show="tree headings", height=6,
                style="Advisor.Treeview",
            )
            tree.heading("#0", text=first_heading)
            tree.column("#0", width=175, anchor="w", stretch=True)
            for column in columns:
                tree.heading(column, text=headings[column])
                tree.column(
                    column, width=widths[column], anchor="center", stretch=True,
                )
            tree.pack(fill="x")
            for tag, color in (
                ("strong", COLORS["green"]), ("good", COLORS["gold"]),
                ("weak", COLORS["red"]), ("blocked", COLORS["muted"]),
            ):
                tree.tag_configure(tag, foreground=color)
            return tree

        opgg_columns = ("rank", "score", "confidence", "winrate", "games", "status")
        self.counter_tree = make_tree(
            "OPGG", opgg_columns,
            {
                "rank": "순위", "score": "종합 점수", "confidence": "신뢰도",
                "winrate": "OP.GG 상대 승률", "games": "OP.GG 표본",
                "status": "추천 가능",
            },
            {
                "rank": 70, "score": 100, "confidence": 90,
                "winrate": 150, "games": 130, "status": 130,
            },
            "OP.GG 후보 챔피언",
        )
        personal_columns = ("games", "record", "winrate", "kda", "vision", "status")
        self.personal_counter_tree = make_tree(
            "PERSONAL", personal_columns,
            {
                "games": "전체 판수", "record": "승 / 패", "winrate": "승률",
                "kda": "평균 KDA", "vision": "평균 시야", "status": "데이터 상태",
            },
            {
                "games": 110, "record": 130, "winrate": 110,
                "kda": 110, "vision": 110, "status": 150,
            },
            "내 챔피언 전적",
        )
        matchup_columns = ("games", "record", "winrate", "confidence", "opgg", "status")
        self.matchup_counter_tree = make_tree(
            "MATCHUP", matchup_columns,
            {
                "games": "맞상대 판수", "record": "승 / 패", "winrate": "내 승률",
                "confidence": "표본 신뢰도", "opgg": "OP.GG 상대 승률",
                "status": "데이터 상태",
            },
            {
                "games": 120, "record": 130, "winrate": 110,
                "confidence": 130, "opgg": 150, "status": 150,
            },
            "현재 상대 기준 내 전적",
        )
        saved_view = self.storage.get_setting("opgg_detail_view") or "OPGG"
        self._opgg_detail_view = (
            saved_view if saved_view in self.opgg_table_frames else "OPGG"
        )
        self._set_opgg_detail_view(self._opgg_detail_view, persist=False)
        self.weak_frame = tk.Frame(panel, bg=COLORS["panel"])
        self.weak_frame.pack(fill="x", pady=(8, 0))

    def _set_opgg_detail_view(
        self, view: str, *, persist: bool = True,
    ) -> None:
        if view not in getattr(self, "opgg_table_frames", {}):
            return
        self._opgg_detail_view = view
        if persist:
            self.storage.set_setting("opgg_detail_view", view)
        for key, frame in self.opgg_table_frames.items():
            if key == view:
                if not frame.winfo_manager():
                    frame.pack(fill="x")
            else:
                frame.pack_forget()
        accents = {
            "OPGG": COLORS["blue"], "PERSONAL": COLORS["green"],
            "MATCHUP": COLORS["purple"],
        }
        for key, button in self.opgg_view_buttons.items():
            self._set_button_selected(button, key == view, accents[key])
        hints = {
            "OPGG": "외부 통계 · 현재 상대 기준 후보별 승률과 표본",
            "PERSONAL": "내 로컬 솔로랭크 · 챔피언 전체 성적",
            "MATCHUP": "내 로컬 솔로랭크 · 현재 상대 챔피언과 만난 기록",
        }
        self.opgg_view_hint.configure(text=hints[view])

    def _build_manual_panel(self, parent: tk.Widget) -> None:
        panel = self._panel(
            parent, "답변 직접 확인·수정 · 필요할 때만", COLORS["purple"],
            outer_padx=0, outer_pady=(0, 0),
        )
        self.prompt_summary_label = tk.Label(
            panel, bg=COLORS["panel"], fg=COLORS["muted"], justify="left", anchor="w",
            font=("Malgun Gothic", 9),
        )
        self.prompt_summary_label.pack(fill="x", pady=(0, 10))
        tk.Label(
            panel,
            text="상단의 ‘답변 붙여넣고 분석’ 버튼이 자동 처리합니다. 형식을 직접 고칠 때만 아래 편집창을 사용하세요.",
            bg=COLORS["panel"], fg=COLORS["blue"],
            font=("Malgun Gothic", 8), anchor="w",
        ).pack(fill="x", pady=(0, 7))
        self.response_text = tk.Text(
            panel, height=4, bg="#0b1220", fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat", bd=0, padx=10, pady=8, wrap="word", font=("Consolas", 9),
            highlightthickness=1, highlightbackground=COLORS["border"],
            selectbackground=COLORS["surface_selected"],
        )
        self.response_text.pack(fill="x")
        self.apply_button = self._button(
            panel, "편집한 내용 분석", self._apply_response, COLORS["green"], filled=True
        )
        self.apply_button.pack(anchor="e", pady=(8, 0))

    def _build_recommendations_panel(self) -> None:
        outer = self._panel(self.content, "2 · 추천 결과", COLORS["green"])
        self.codex_recommendations_outer = outer.master
        self.champion_action_status = tk.Label(
            outer,
            text="추천 챔피언의 선택/픽은 버튼을 누를 때마다 최신 롤 세션을 재확인합니다.",
            bg=COLORS["panel"], fg=COLORS["muted"], anchor="w",
            font=("Malgun Gothic", 9, "bold"),
        )
        self.champion_action_status.pack(fill="x", pady=(0, 9))
        self.cards_frame = tk.Frame(outer, bg=COLORS["panel"])
        self.cards_frame.pack(fill="x")

    def _apply_codex_recommendation_visibility(self) -> None:
        """Show the pick-recommendation UI only after explicit opt-in."""
        enabled = bool(getattr(self, "codex_recommendations_enabled", False))
        workflow = getattr(self, "codex_workflow_frame", None)
        results = getattr(self, "codex_recommendations_outer", None)
        detail_section = getattr(self, "selection_detail_section", None)
        notebook = getattr(self, "selection_detail_notebook", None)
        chat_tab = getattr(self, "selection_chat_tab", None)
        if isinstance(workflow, tk.Widget):
            if enabled:
                if not workflow.winfo_manager():
                    workflow.pack(fill="x", pady=(12, 0))
            elif workflow.winfo_manager():
                workflow.pack_forget()
        if isinstance(results, tk.Widget):
            if enabled:
                if not results.winfo_manager():
                    pack_options: dict[str, object] = {
                        "fill": "x", "padx": 22, "pady": (0, 11),
                    }
                    if isinstance(detail_section, tk.Widget):
                        pack_options["before"] = detail_section
                    results.pack(**pack_options)
            elif results.winfo_manager():
                results.pack_forget()
        if isinstance(notebook, ttk.Notebook) and isinstance(chat_tab, tk.Widget):
            try:
                if enabled:
                    notebook.add(chat_tab, text="ChatGPT 답변 편집")
                else:
                    if notebook.select() == str(chat_tab):
                        notebook.select(self.selection_synergy_tab)
                    notebook.hide(chat_tab)
            except tk.TclError:
                pass
        heading = getattr(self, "selection_detail_heading_label", None)
        if isinstance(heading, tk.Label):
            heading.configure(text="3 · 상세 분석" if enabled else "2 · 상세 분석")
        draft_heading = getattr(self, "draft_panel_title_label", None)
        if isinstance(draft_heading, tk.Label):
            draft_heading.configure(
                text=(
                    "1 · 현재 드래프트와 추천 요청"
                    if enabled else "1 · 현재 드래프트"
                )
            )

    def _build_play_panel(self) -> None:
        panel = self._panel(self.play_content, "현재 게임 플레이어", COLORS["gold"])
        top = tk.Frame(panel, bg=COLORS["panel"])
        top.pack(fill="x", pady=(0, 10))
        self.live_game_label = tk.Label(
            top, text=self._tr("게임 시작을 기다리는 중"), bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 10, "bold"),
        )
        self.live_game_label.pack(side="left")
        self.live_profile_status = tk.Label(
            top, text="", bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 9)
        )
        self.live_profile_status.pack(side="right")
        self.previous_play_button = self._button(
            top,
            "이전 게임 플레이탭 보기",
            self._show_previous_play,
            COLORS["blue"],
        )
        # Reveal this only when no current game exists and a frozen board does.
        self.previous_play_button.pack(side="right", padx=(0, 10))
        self.previous_play_button.pack_forget()
        self.live_duo_status = tk.Label(
            panel,
            text=self._tr("DUO: 현재 10명의 최근 100경기 교집합 확인 · 카드 수치는 로컬/Riot 결합"),
            bg=COLORS["panel"], fg=COLORS["orange"], font=("Malgun Gothic", 8),
        )
        self.live_duo_status.pack(anchor="w", pady=(0, 8))
        self.live_duo_legend = tk.Frame(panel, bg=COLORS["panel"])
        self.live_duo_legend.pack(fill="x", pady=(0, 8))
        self._play_duo_legend_signature = ""
        summary = tk.Frame(panel, bg=COLORS["panel"])
        summary.pack(fill="x", pady=(0, 13))
        self.play_metrics: dict[str, tuple[tk.Label, tk.Label]] = {}
        for index, (key, title, accent) in enumerate((
            ("ally", "아군 시즌 평균", COLORS["green"]),
            ("enemy", "적군 시즌 평균", COLORS["red"]),
            ("cache", "확인된 플레이어", COLORS["blue"]),
            ("duo", "DUO 신호", COLORS["orange"]),
            ("matchup", "OP.GG 라인 상성", COLORS["purple"]),
            ("prediction", "예상 승률", COLORS["gold"]),
        )):
            outer, value, detail = self._mini_metric(summary, title, accent)
            self.play_metrics[key] = (value, detail)
            outer.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 5, 5))
        board = tk.Frame(panel, bg=COLORS["panel"])
        board.pack(fill="x")
        ally_heading = tk.Frame(board, bg=COLORS["panel"])
        ally_heading.pack(fill="x", pady=(0, 6))
        tk.Label(
            ally_heading, text=self._tr("아군  ·  TOP   JGL   MID   ADC   SUP"),
            bg=COLORS["panel"], fg=COLORS["green"],
            font=("Malgun Gothic", 11, "bold"),
        ).pack(side="left")
        tk.Label(
            ally_heading, text=self._tr("카드를 가로로 비교하세요"),
            bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
        ).pack(side="right")
        self.live_allies_frame = tk.Frame(board, bg=COLORS["panel"])
        self.live_allies_frame.pack(fill="x")
        divider = tk.Frame(board, bg=COLORS["border"], height=1)
        divider.pack(fill="x", pady=9)
        enemy_heading = tk.Frame(board, bg=COLORS["panel"])
        enemy_heading.pack(fill="x", pady=(0, 6))
        tk.Label(
            enemy_heading, text=self._tr("적군  ·  TOP   JGL   MID   ADC   SUP"),
            bg=COLORS["panel"], fg=COLORS["red"],
            font=("Malgun Gothic", 11, "bold"),
        ).pack(side="left")
        tk.Label(
            enemy_heading, text=self._tr("빨강은 패배·낮은 승률, 주황은 낮은 표본"),
            bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
        ).pack(side="right")
        self.live_enemies_frame = tk.Frame(board, bg=COLORS["panel"])
        self.live_enemies_frame.pack(fill="x")
        for frame in (self.live_allies_frame, self.live_enemies_frame):
            for column in range(5):
                frame.grid_columnconfigure(column, weight=1, uniform="player_cards")

        insights = self._panel(
            self.play_content, "라인 매치업 · 정글 플레이 인사이트", COLORS["blue"]
        )
        insight_header = tk.Frame(insights, bg=COLORS["panel"])
        insight_header.pack(fill="x", pady=(0, 9))
        tk.Label(
            insight_header,
            text=self._tr("게임 승률과 라인전 지표를 분리하고, 저장된 솔로랭크 행동 표본을 비교합니다."),
            bg=COLORS["panel"], fg=COLORS["text"],
            font=("Malgun Gothic", 9, "bold"), anchor="w",
        ).pack(side="left")
        self.play_insight_status = tk.Label(
            insight_header, text=self._tr("게임 시작 후 자동 분석"), bg=COLORS["panel"],
            fg=COLORS["muted"], font=("Malgun Gothic", 8),
        )
        self.play_insight_status.pack(side="right")
        self.play_prediction_frame = tk.Frame(
            insights, bg=COLORS["surface"], padx=12, pady=9,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.play_prediction_value = tk.Label(
            self.play_prediction_frame, text="", bg=COLORS["surface"],
            fg=COLORS["gold"], font=("Malgun Gothic", 11, "bold"),
        )
        self.play_prediction_value.pack(side="left")
        self.play_prediction_detail = tk.Label(
            self.play_prediction_frame, text="", bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Malgun Gothic", 7, "bold"),
            wraplength=900, justify="right", anchor="e",
        )
        self.play_prediction_detail.pack(side="right")
        self.play_insight_body = tk.Frame(insights, bg=COLORS["panel"])
        self.play_insight_body.pack(fill="x")
        self.play_insight_sections: dict[str, tk.Frame] = {}
        for key in ("lane", "jungle_plan", "opponent", "mine"):
            section = tk.Frame(self.play_insight_body, bg=COLORS["panel"])
            section.pack(fill="x")
            self.play_insight_sections[key] = section

    def _mini_metric(
        self, parent: tk.Widget, title: str, accent: str
    ) -> tuple[tk.Frame, tk.Label, tk.Label]:
        outer = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        card = tk.Frame(outer, bg=COLORS["surface"], padx=9, pady=5)
        card.pack(fill="both", expand=True)
        tk.Label(
            card, text=self._tr(title), bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w")
        row = tk.Frame(card, bg=COLORS["surface"])
        row.pack(fill="x")
        value = tk.Label(
            row, text="--", bg=COLORS["surface"], fg=accent,
            font=("Malgun Gothic", 10, "bold"),
        )
        value.pack(side="left")
        detail = tk.Label(
            row, text="", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7),
        )
        detail.pack(side="right")
        return outer, value, detail

    def _build_history_panel(self) -> None:
        panel = self._panel(self.history_content, "내 솔로랭크 전적", COLORS["purple"])
        top = tk.Frame(panel, bg=COLORS["panel"])
        top.pack(fill="x", pady=(0, 10))
        identity = self.storage.get_setting("riot_game_name") or "Riot 계정 미확인"
        tag = self.storage.get_setting("riot_tag_line")
        self.history_identity_label = tk.Label(
            top, text=f"{identity}{'#' + tag if tag else ''}", bg=COLORS["panel"],
            fg=COLORS["gold"], font=("Malgun Gothic", 14, "bold"),
        )
        self.history_identity_label.pack(side="left")
        self.history_status_label = tk.Label(
            top, text="로컬 전적 준비 중", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        )
        self.history_status_label.pack(side="left", padx=12)
        self._button(
            top, "통계 다시 계산", lambda: self._ensure_history_loaded(force=True), COLORS["blue"]
        ).pack(side="right")
        self._button(
            top, "Riot 전적 갱신", self._sync_riot, COLORS["green"]
        ).pack(side="right", padx=(0, 8))

        summary = tk.Frame(panel, bg=COLORS["panel"])
        summary.pack(fill="x", pady=(0, 5))
        self.history_metrics: dict[str, tuple[tk.Label, tk.Label]] = {}
        for index, (key, title, accent) in enumerate((
            ("rank", "현재 솔로랭크", COLORS["gold"]),
            ("games", "로컬 저장 경기", COLORS["blue"]),
            ("recent", "최근 20경기", COLORS["green"]),
            ("kda", "전체 KDA", COLORS["purple"]),
            ("vision", "평균 시야", COLORS["orange"]),
            ("prediction", "최근 20 예측", COLORS["gold"]),
        )):
            outer, value, detail = self._mini_metric(summary, title, accent)
            self.history_metrics[key] = (value, detail)
            outer.pack(
                side="left", fill="x", expand=True,
                padx=(0 if index == 0 else 4, 4),
            )

        history_recent_strip = tk.Frame(panel, bg=COLORS["chip"], padx=6, pady=4)
        history_recent_strip.pack(fill="x", pady=(0, 12))
        self.history_lp_strip_label = tk.Label(
            history_recent_strip,
            text=self._tr("LP 변동은 새 솔로랭크부터 정확히 기록합니다."),
            bg=COLORS["chip"], fg=COLORS["muted"], padx=4,
            anchor="w", font=("Malgun Gothic", 7, "bold"),
        )
        self.history_lp_strip_label.pack(side="left", fill="x", expand=True)
        self.history_recent_champions_frame = tk.Frame(
            history_recent_strip, bg=COLORS["chip"],
        )
        self.history_recent_champions_frame.pack(side="right")

        body = tk.Frame(panel, bg=COLORS["panel"])
        body.pack(fill="both", expand=True)
        left = tk.Frame(
            body, bg=COLORS["panel_2"], width=330, padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)
        tk.Label(
            left, text=self._tr("챔피언 성능"), bg=COLORS["panel_2"], fg=COLORS["text"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            left, text=self._tr("저장된 솔로랭크 전체 표본"), bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7),
        ).pack(anchor="w", pady=(1, 6))
        position_filters = tk.Frame(left, bg=COLORS["panel_2"])
        position_filters.pack(fill="x", pady=(0, 7))
        self.history_position_buttons: dict[str, tk.Button] = {}
        for key, label in (
            ("ALL", "전체"), ("TOP", "TOP"), ("JUNGLE", "JGL"),
            ("MIDDLE", "MID"), ("BOTTOM", "ADC"), ("SUPPORT", "SUP"),
        ):
            accent = (
                COLORS["purple"] if key == "ALL"
                else POSITION_BADGE_COLORS[key]
            )
            button = self._button(
                position_filters, label,
                lambda selected=key: self._set_history_position_filter(selected),
                accent, width=4,
            )
            button.configure(
                padx=3, pady=3, font=("Malgun Gothic", 6, "bold")
            )
            button.pack(side="left", fill="x", expand=True, padx=(0, 3))
            self.history_position_buttons[key] = button
        self.history_champions_frame = tk.Frame(left, bg=COLORS["panel_2"])
        self.history_champions_frame.pack(fill="x")

        right = tk.Frame(
            body, bg=COLORS["panel_2"], padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        right.pack(side="left", fill="both", expand=True)
        match_header = tk.Frame(right, bg=COLORS["panel_2"])
        match_header.pack(fill="x", pady=(0, 8))
        tk.Label(
            match_header, text=self._tr("최근 경기"), bg=COLORS["panel_2"], fg=COLORS["text"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(side="left")
        filters = tk.Frame(match_header, bg=COLORS["panel_2"])
        filters.pack(side="left", padx=(12, 0))
        self.history_filter_buttons: dict[str, tk.Button] = {}
        for key, label, accent in (
            ("ALL", "전체", COLORS["purple"]),
            ("WIN", "승리", COLORS["green"]),
            ("LOSS", "패배", COLORS["red"]),
        ):
            button = self._button(
                filters, label,
                lambda selected=key: self._set_history_result_filter(selected),
                accent, width=6,
            )
            button.configure(padx=6, pady=3, font=("Malgun Gothic", 7, "bold"))
            button.pack(side="left", padx=(0, 4))
            self.history_filter_buttons[key] = button
        tk.Label(
            match_header, text=self._tr("경기 상세에서 양 팀 10명·전투·시야·아이템 비교"),
            bg=COLORS["panel_2"], fg=COLORS["muted"], font=("Malgun Gothic", 7),
        ).pack(side="right")
        self.history_matches_frame = tk.Frame(right, bg=COLORS["panel_2"])
        self.history_matches_frame.pack(fill="x")
        self.history_more_button = self._button(
            right, "경기 더 보기", self._show_more_history, COLORS["purple"]
        )
        self.history_more_button.pack(anchor="center", pady=(9, 0))

    def _build_build_panel(self) -> None:
        selector = self._panel(
            self.build_content, "챔피언 빌드 선택", COLORS["gold"]
        )
        row = tk.Frame(selector, bg=COLORS["panel"])
        row.pack(fill="x")
        tk.Label(
            row, text=self._tr("챔피언"), bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 9))
        self.build_champion_var = tk.StringVar()
        self.build_champion_combo = ttk.Combobox(
            row, textvariable=self.build_champion_var, state="readonly", width=30,
            font=("Malgun Gothic", 10), style="Advisor.TCombobox",
        )
        self.build_champion_combo.pack(side="left", ipady=5)
        self.build_champion_combo.bind(
            "<<ComboboxSelected>>", self._on_build_champion_selected
        )
        self._refresh_build_champion_values()
        self.build_position_label = tk.Label(
            row, text="", bg=COLORS["panel"], fg=COLORS["blue"],
            font=("Malgun Gothic", 9, "bold"),
        )
        self.build_position_label.pack(side="left", padx=12)
        self.build_refresh_button = self._button(
            row, "OP.GG 빌드 불러오기", self._refresh_build_guide, COLORS["blue"]
        )
        self.build_refresh_button.pack(side="right")
        self.build_bulk_button = self._button(
            row, "전체 챔피언 빌드 로컬 저장",
            self._toggle_bulk_build_download, COLORS["green"], width=24,
        )
        self.build_bulk_button.pack(side="right", padx=(0, 8))
        if self.demo:
            self.build_bulk_button.configure(state="disabled")
        self.build_status_label = tk.Label(
            selector,
            text=self._tr("챔피언을 고르면 캐시를 먼저 읽고, 없으면 OP.GG에서 한 번 받아옵니다."),
            bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
            anchor="w", justify="left",
        )
        self.build_status_label.pack(fill="x", pady=(9, 0))
        self.build_bulk_progress = ttk.Progressbar(
            selector, orient="horizontal", mode="determinate", maximum=1, value=0,
            style="Build.Horizontal.TProgressbar",
        )
        self.build_bulk_status_label = tk.Label(
            selector,
            text=self._tr("전체 저장은 5개 포지션의 실제 통계 챔피언만 순차 저장합니다."),
            bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
            anchor="w", justify="left",
        )
        self.build_bulk_status_label.pack(fill="x", pady=(5, 0))

        actions = self._panel(
            self.build_content, "빠른 적용 · 화면 위에서 바로", COLORS["green"]
        )
        action_header = tk.Frame(actions, bg=COLORS["panel"])
        action_header.pack(fill="x", pady=(0, 8))
        tk.Label(
            action_header,
            text=self._tr("원하는 항목만 적용하거나 전체를 한 번에 적용할 수 있습니다."),
            bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        ).pack(side="left")
        self.build_apply_status = tk.Label(
            action_header, text="", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8, "bold"), anchor="e",
        )
        self.build_apply_status.pack(side="right", fill="x", expand=True)
        quick_row = tk.Frame(actions, bg=COLORS["panel"])
        quick_row.pack(fill="x")
        self.build_apply_runes_button = self._button(
            quick_row, "룬 적용", lambda: self._apply_build_component("runes"),
            COLORS["purple"], width=15,
        )
        self.build_apply_spells_button = self._button(
            quick_row, "스펠 적용", lambda: self._apply_build_component("spells"),
            COLORS["blue"], width=15,
        )
        self.build_apply_items_button = self._button(
            quick_row, "아이템 적용", lambda: self._apply_build_component("items"),
            COLORS["gold"], width=15,
        )
        self.build_apply_all_button = self._button(
            quick_row, "선택 빌드 전체 적용",
            lambda: self._apply_build_component("all"), COLORS["green"],
            width=22, filled=True,
        )
        for button in (
            self.build_apply_runes_button, self.build_apply_spells_button,
            self.build_apply_items_button,
        ):
            button.pack(side="left", padx=(0, 7))
        self.build_apply_all_button.pack(side="right")
        self.build_quick_apply_buttons = [
            self.build_apply_runes_button, self.build_apply_spells_button,
            self.build_apply_items_button, self.build_apply_all_button,
        ]
        self.build_apply_buttons: list[tk.Button] = list(
            self.build_quick_apply_buttons
        )

        guide_panel = self._panel(
            self.build_content, "룬 · 스펠 · 스킬 · 아이템", COLORS["purple"]
        )
        self.build_guide_summary = tk.Label(
            guide_panel, text=self._tr("빌드 데이터 없음"), bg=COLORS["panel"],
            fg=COLORS["muted"], anchor="w", justify="left",
            font=("Malgun Gothic", 9),
        )
        self.build_guide_summary.pack(fill="x", pady=(0, 10))
        columns = tk.Frame(guide_panel, bg=COLORS["panel"])
        columns.pack(fill="x")
        columns.grid_columnconfigure(0, weight=0, minsize=210)
        # Keep the three data columns in a strict ratio. Without a uniform
        # group, a longer selected rune name changes the requested width and
        # visibly pushes the spell/item columns sideways on every click.
        columns.grid_columnconfigure(1, weight=16, uniform="build_data")
        columns.grid_columnconfigure(2, weight=10, uniform="build_data")
        columns.grid_columnconfigure(3, weight=14, uniform="build_data")
        self.build_presets_frame = tk.Frame(
            columns, bg=COLORS["panel_2"], padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.build_presets_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.build_runes_frame = tk.Frame(
            columns, bg=COLORS["panel_2"], padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.build_runes_frame.grid(row=0, column=1, sticky="nsew", padx=5)
        self.build_spells_frame = tk.Frame(
            columns, bg=COLORS["panel_2"], padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.build_spells_frame.grid(row=0, column=2, sticky="nsew", padx=5)
        self.build_items_frame = tk.Frame(
            columns, bg=COLORS["panel_2"], padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.build_items_frame.grid(row=0, column=3, sticky="nsew", padx=(5, 0))

    def _chip(self, parent: tk.Widget, text: str, color: str = COLORS["text"],
              command: Callable[[], None] | None = None, selected: bool = False,
              champion_id: str | None = None,
              icon_panel: str = "opgg") -> tk.Widget:
        cls = tk.Button if command else tk.Label
        icon = (
            self.icon_cache.get(
                champion_id, 32, self._selection_icon_ready(icon_panel)
            )
            if champion_id else None
        )
        widget = cls(
            parent, text=self._tr(text), bg="#24466e" if selected else COLORS["chip"], fg=color,
            relief="flat", bd=0, padx=10, pady=6, font=("Malgun Gothic", 9),
            image=icon or "", compound="left",
            **({"command": command, "cursor": "hand2", "activebackground": "#315c8e",
                "activeforeground": color} if command else {}),
        )
        widget.pack(side="left", padx=(0, 6), pady=2)
        return widget

    @staticmethod
    def _clear(frame: tk.Widget) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _tr(self, text: object) -> str:
        return translate_text(
            text,
            getattr(self, "ui_language", "ko"),
            getattr(self, "_champion_name_translations", {}),
        )

    def _text(self, key: str, **values: object) -> str:
        return localized_text(
            key, getattr(self, "ui_language", "ko"), **values,
        )

    def _position_text(self, position: str | None) -> str:
        normalized = str(position or "UNKNOWN").upper()
        key = {
            "TOP": "role.top", "JUNGLE": "role.jungle",
            "MIDDLE": "role.middle", "MID": "role.middle",
            "BOTTOM": "role.bottom", "ADC": "role.bottom",
            "SUPPORT": "role.support", "UTILITY": "role.support",
        }.get(normalized, "role.unknown")
        return self._text(key)

    def _champion_text(
        self, champion_id: str | None, korean_name: str | None = None,
    ) -> str:
        if not champion_id:
            return self._text("common.unknown")
        if getattr(self, "ui_language", "ko") == "en":
            return self.registry.normalize_id(champion_id)
        return korean_name or self.registry.ko_name(champion_id)

    def _rune_name_text(self, perk_id: int, fallback: str = "") -> str:
        if getattr(self, "ui_language", "ko") == "en":
            return RUNE_NAMES_EN.get(int(perk_id or 0), f"Rune {int(perk_id or 0)}")
        option = self.rune_catalog.perk(perk_id)
        return option.name if option else (fallback or f"룬 #{perk_id}")

    def _rune_style_text(self, style_id: int, fallback: str = "") -> str:
        if getattr(self, "ui_language", "ko") == "en":
            return RUNE_STYLE_NAMES_EN.get(
                int(style_id or 0), f"Rune path {int(style_id or 0)}",
            )
        style = self.rune_catalog.style(style_id)
        return style.name if style else (fallback or f"계열 #{style_id}")

    def _asset_name_text(self, asset: BuildAsset, kind: str) -> str:
        if getattr(self, "ui_language", "ko") != "en":
            return asset.name
        if kind == "item":
            return self.item_icon_cache.localized_item_name(
                asset.asset_id, self.ui_language, asset.name,
            )
        if kind == "rune":
            return self._rune_name_text(asset.asset_id, asset.name)
        if kind == "spell":
            return SUMMONER_SPELL_NAMES_EN.get(
                int(asset.asset_id or 0), f"Summoner spell {asset.asset_id}",
            )
        return asset.name

    def _asset_tooltip_text(self, asset: BuildAsset, kind: str) -> str:
        if kind == "item":
            return self.item_icon_cache.localized_tooltip_text(
                asset.asset_id, getattr(self, "ui_language", "ko"),
            )
        name = self._asset_name_text(asset, kind)
        if getattr(self, "ui_language", "ko") == "en":
            label = "Rune" if kind == "rune" else "Summoner spell"
            return f"{name}\n{label} ID {asset.asset_id}"
        if kind == "rune":
            return self.rune_catalog.tooltip_text(asset.asset_id, asset.name)
        return f"{name}\n소환사 주문"

    def _build_stat_text(
        self, games: int | None, win_rate: float | None,
    ) -> str:
        if games and win_rate is not None:
            return self._text("build.stats", games=games, win_rate=win_rate)
        if games:
            return self._text("build.stats_games", games=games)
        if win_rate is not None:
            return self._text("build.stats_rate", win_rate=win_rate)
        return self._text("build.stats_none")

    def _streak_text(self, value: int, prefix: str = "") -> str:
        if abs(value) < 2:
            return ""
        count = f"{abs(value)}+" if abs(value) >= 10 else str(abs(value))
        return self._text(
            "play.streak.win" if value > 0 else "play.streak.loss",
            prefix=prefix, count=count,
        )

    def _games_text(self, games: int | None) -> str:
        if not games:
            return self._tr("표본 미제공")
        return (
            f"{games:,} games" if getattr(self, "ui_language", "ko") == "en"
            else f"{games:,}게임"
        )

    def _matchup_label_text(self, win_rate: float | None) -> str:
        return self._tr(lane_matchup_label(win_rate))

    def _on_widget_mapped(self, event: tk.Event) -> None:
        if self.ui_language != "en":
            return
        widget = getattr(event, "widget", None)
        if not isinstance(widget, tk.Misc):
            return
        self._language_mapped_widgets.add(widget)
        if self._language_map_after_id is None:
            try:
                # Translate the complete map burst before Tk's next paint.
                # The previous 24 ms delay visibly showed Korean widgets and
                # then replaced them with English on every card rebuild.
                self._language_map_after_id = self.root.after_idle(
                    self._flush_mapped_widget_translations,
                )
            except tk.TclError:
                self._language_map_after_id = None

    def _flush_mapped_widget_translations(self) -> None:
        self._language_map_after_id = None
        widgets = tuple(self._language_mapped_widgets)
        self._language_mapped_widgets.clear()
        for widget in widgets:
            self._translate_widget(widget)

    def _translate_widget(self, widget: tk.Misc) -> None:
        try:
            if not widget.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            current = str(widget.cget("text"))
        except (AttributeError, KeyError, tk.TclError):
            current = ""
        if current:
            source = str(getattr(widget, "_advisor_i18n_source", current))
            previous = str(getattr(widget, "_advisor_i18n_rendered", ""))
            # Existing labels are frequently updated in place by live data.
            # Treat anything other than our last rendering as fresh Korean
            # source copy, then preserve it for a future switch back to Korean.
            if not previous or current != previous:
                source = current
            rendered = self._tr(source)
            if rendered != current:
                try:
                    widget.configure(text=rendered)
                except (AttributeError, tk.TclError):
                    pass
            setattr(widget, "_advisor_i18n_source", source)
            setattr(widget, "_advisor_i18n_rendered", rendered)
            if self.ui_language == "en":
                self._language_tracked_widgets.add(widget)
        if isinstance(widget, ttk.Treeview):
            if self.ui_language == "en":
                self._language_tracked_widgets.add(widget)
            sources = dict(getattr(widget, "_advisor_i18n_headings", {}))
            try:
                columns = ("#0", *tuple(widget.cget("columns")))
            except tk.TclError:
                columns = ()
            for column in columns:
                try:
                    current_heading = str(widget.heading(column, "text"))
                except tk.TclError:
                    continue
                source_heading = sources.get(column, current_heading)
                previous_heading = self._tr(source_heading)
                if current_heading not in {source_heading, previous_heading}:
                    source_heading = current_heading
                sources[column] = source_heading
                translated_heading = self._tr(source_heading)
                if translated_heading != current_heading:
                    try:
                        widget.heading(column, text=translated_heading)
                    except tk.TclError:
                        pass
            setattr(widget, "_advisor_i18n_headings", sources)
            item_sources = dict(
                getattr(widget, "_advisor_i18n_items", {})
            )
            live_items: set[str] = set()

            def translate_items(parent: str = "") -> None:
                try:
                    item_ids = tuple(widget.get_children(parent))
                except tk.TclError:
                    return
                for item_id in item_ids:
                    item_key = str(item_id)
                    live_items.add(item_key)
                    try:
                        current_text = str(widget.item(item_id, "text") or "")
                        current_values = tuple(widget.item(item_id, "values") or ())
                    except tk.TclError:
                        continue
                    saved = item_sources.get(item_key)
                    if saved:
                        source_text, source_values, rendered_text, rendered_values = saved
                    else:
                        source_text = current_text
                        source_values = current_values
                        rendered_text = current_text
                        rendered_values = current_values
                    # Live rows can be updated in place.  Only treat the row as
                    # fresh source copy when it differs from our last rendering;
                    # this also makes an English -> Korean switch reversible.
                    if current_text != rendered_text:
                        source_text = current_text
                    if current_values != tuple(rendered_values):
                        source_values = current_values
                    translated_text = self._tr(source_text)
                    translated_values = tuple(
                        self._tr(value) if isinstance(value, str) else value
                        for value in source_values
                    )
                    if (
                        translated_text != current_text
                        or translated_values != current_values
                    ):
                        try:
                            widget.item(
                                item_id, text=translated_text,
                                values=translated_values,
                            )
                        except tk.TclError:
                            pass
                    item_sources[item_key] = (
                        source_text, source_values,
                        translated_text, translated_values,
                    )
                    translate_items(item_id)

            translate_items()
            setattr(
                widget, "_advisor_i18n_items",
                {
                    key: value for key, value in item_sources.items()
                    if key in live_items
                },
            )
        if isinstance(widget, ttk.Notebook):
            if self.ui_language == "en":
                self._language_tracked_widgets.add(widget)
            tab_sources = dict(
                getattr(widget, "_advisor_i18n_tabs", {})
            )
            try:
                tab_ids = tuple(widget.tabs())
            except tk.TclError:
                tab_ids = ()
            live_tabs: set[str] = set()
            for tab_id in tab_ids:
                tab_key = str(tab_id)
                live_tabs.add(tab_key)
                try:
                    current_tab_text = str(widget.tab(tab_id, "text") or "")
                except tk.TclError:
                    continue
                source_tab, rendered_tab = tab_sources.get(
                    tab_key, (current_tab_text, current_tab_text),
                )
                if current_tab_text != rendered_tab:
                    source_tab = current_tab_text
                translated_tab = self._tr(source_tab)
                if translated_tab != current_tab_text:
                    try:
                        widget.tab(tab_id, text=translated_tab)
                    except tk.TclError:
                        pass
                tab_sources[tab_key] = (source_tab, translated_tab)
            setattr(
                widget, "_advisor_i18n_tabs",
                {
                    key: value for key, value in tab_sources.items()
                    if key in live_tabs
                },
            )

    def _translate_widget_tree(self, widget: tk.Misc) -> None:
        self._translate_widget(widget)
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            self._translate_widget_tree(child)

    def _update_fixed_language_texts(self) -> None:
        notebook = getattr(self, "notebook", None)
        if notebook is not None:
            fixed_tabs = (
                (getattr(self, "selection_tab", None), "1  선택창"),
                (getattr(self, "play_tab", None), "2  플레이"),
                (getattr(self, "history_tab", None), "3  전적"),
                (getattr(self, "build_tab", None), "4  빌드 적용"),
            )
            for tab, source in fixed_tabs:
                if tab is None:
                    continue
                try:
                    notebook.tab(tab, text=self._tr(source))
                except tk.TclError:
                    pass
        history_notebook = getattr(self, "history_notebook", None)
        history_home = getattr(self, "history_home_tab", None)
        if history_notebook is not None and history_home is not None:
            try:
                history_notebook.tab(history_home, text=self._tr("내 전적"))
            except tk.TclError:
                pass

    def _active_language_roots(self) -> tuple[tk.Misc, ...]:
        roots: list[tk.Misc] = []
        header = getattr(self, "header_frame", None)
        if isinstance(header, tk.Misc):
            roots.append(header)
        active = self._current_main_tab_index() if hasattr(self, "notebook") else 0
        active_root = {
            0: getattr(self, "selection_content", None),
            1: getattr(self, "play_content", None),
            2: getattr(self, "history_tab", None),
            3: getattr(self, "build_content", None),
        }.get(active)
        if isinstance(active_root, tk.Misc):
            roots.append(active_root)
        try:
            roots.extend(
                child for child in self.root.winfo_children()
                if isinstance(child, tk.Toplevel)
            )
        except tk.TclError:
            pass
        return tuple(dict.fromkeys(roots))

    def _apply_language(self, *, full: bool = False) -> None:
        self._update_fixed_language_texts()
        roots: tuple[tk.Misc, ...] = (
            (self.root,) if full else self._active_language_roots()
        )
        for root in roots:
            self._translate_widget_tree(root)

    def _schedule_language_refresh(self) -> None:
        if (
            getattr(self, "ui_language", "ko") != "en"
            or getattr(self, "_language_refresh_after_id", None) is not None
        ):
            return
        try:
            self._language_refresh_after_id = self.root.after(
                45, self._flush_language_refresh,
            )
        except tk.TclError:
            self._language_refresh_after_id = None

    def _flush_language_refresh(self) -> None:
        self._language_refresh_after_id = None
        self._update_fixed_language_texts()
        tracked = tuple(getattr(self, "_language_tracked_widgets", ()))
        for widget in tracked:
            try:
                exists = bool(widget.winfo_exists())
            except tk.TclError:
                exists = False
            if not exists:
                self._language_tracked_widgets.discard(widget)
                continue
            self._translate_widget(widget)

    def _render_all(self) -> None:
        self.draft.refresh_snapshot_id()
        active = self._current_main_tab_index()
        if active == 0:
            self._render_selection()
            self._schedule_language_refresh()
            return
        self._render_header()
        if active == 1:
            self._render_play()
        elif active == 2:
            self._render_history()
        elif active == 3:
            self._render_build()
        self._schedule_language_refresh()

    def _render_selection(self) -> None:
        if self._current_main_tab_index() != 0:
            self._render_header()
            return
        self.draft.refresh_snapshot_id()
        self._render_header()
        self._render_draft()
        self._render_hover_matchup_card()
        self._render_synergy()
        self._render_opgg_meta()
        self._render_opgg()
        self._render_recommendations()
        self._render_prompt_summary()

    def _selection_panel_needs_render(self, name: str, *values: object) -> bool:
        signature = repr((
            self._selection_panel_revisions.get(name, 0),
            values,
        ))
        if self._selection_panel_signatures.get(name) == signature:
            return False
        self._selection_panel_signatures[name] = signature
        return True

    def _invalidate_selection_panels(self, *names: str) -> None:
        targets = names or ("draft", "synergy", "meta", "opgg", "recommendations")
        for name in targets:
            self._selection_panel_revisions[name] = (
                self._selection_panel_revisions.get(name, 0) + 1
            )
        self._schedule_selection_render()

    def _selection_icon_ready(self, panel: str) -> Callable[[], None]:
        return lambda: self._schedule_selection_asset_render(panel)

    def _schedule_selection_asset_render(self, panel: str) -> None:
        """Coalesce icon arrivals so a table is not rebuilt once per icon."""
        self._selection_asset_pending_panels.add(panel)
        if self._selection_asset_after_id:
            try:
                self.root.after_cancel(self._selection_asset_after_id)
            except tk.TclError:
                return

        def render() -> None:
            self._selection_asset_after_id = None
            panels = tuple(self._selection_asset_pending_panels)
            self._selection_asset_pending_panels.clear()
            if panels:
                self._invalidate_selection_panels(*panels)

        # OP.GG tables commonly request ten icons at once. Waiting for the
        # burst to settle prevents ten visible destroy/recreate cycles.
        self._selection_asset_after_id = self.root.after(520, render)

    def _invalidate_all_champion_icon_panels(self) -> None:
        self._invalidate_selection_panels()
        self._schedule_history_render()

    def _schedule_selection_render(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._selection_render_scheduled = True
        if self._selection_render_after_id:
            try:
                self.root.after_cancel(self._selection_render_after_id)
            except tk.TclError:
                self._selection_render_after_id = None

        def render() -> None:
            self._selection_render_scheduled = False
            self._selection_render_after_id = None
            if not self._closing:
                self._render_selection()

        # LCU, OP.GG and icon callbacks commonly arrive a few milliseconds
        # apart. A short trailing debounce paints the newest draft once instead
        # of showing every intermediate state as a flash.
        self._selection_render_after_id = self.root.after(110, render)

    def _schedule_play_render(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._play_render_scheduled = True
        if self._play_render_after_id:
            try:
                self.root.after_cancel(self._play_render_after_id)
            except tk.TclError:
                self._play_render_after_id = None

        def render() -> None:
            self._play_render_scheduled = False
            self._play_render_after_id = None
            if not self._closing:
                self._render_play()

        # Ten profile, OP.GG and matchup results arrive independently.  Hold a
        # very small window so each burst updates cards once, while keeping the
        # first visible roster comfortably below human reaction time.
        self._play_render_after_id = self.root.after(140, render)

    def _schedule_play_insight_render(self, delay_ms: int = 380) -> None:
        """Trailing-debounce the large lower play analysis tree.

        Player, OP.GG and lane-cache results arrive independently in a short
        burst.  Rebuilding the several-hundred-widget insight tree for every
        result blocks Tk's paint loop, so only the last state in that burst is
        rendered.  The ten player cards and top summary remain on their faster
        path.
        """
        if self._play_insight_after_id:
            try:
                self.root.after_cancel(self._play_insight_after_id)
            except tk.TclError:
                self._play_insight_after_id = None

        def render() -> None:
            self._play_insight_after_id = None
            if self._current_main_tab_index() == 1:
                self._render_play_insights()

        self._play_insight_after_id = self.root.after(delay_ms, render)

    def _invalidate_play_cards(self) -> None:
        """Force one card rebuild after an asynchronously loaded icon changes."""
        self._play_card_signatures.clear()
        self._schedule_play_render()

    def _invalidate_play_card(self, key: str) -> None:
        self._play_card_signatures.pop(key, None)
        self._schedule_play_render()

    def _schedule_history_render(self) -> None:
        self._history_asset_revision += 1
        self._history_render_scheduled = True
        if self._history_render_after_id:
            try:
                self.root.after_cancel(self._history_render_after_id)
            except tk.TclError:
                pass

        def render() -> None:
            self._history_render_scheduled = False
            self._history_render_after_id = None
            self._render_history()

        # Item/champion icons arrive in a burst. A trailing refresh collapses
        # dozens of callbacks into one history redraw instead of flashing once
        # per downloaded asset.
        self._history_render_after_id = self.root.after(650, render)

    def _refresh_build_champion_values(self) -> None:
        values: list[str] = []
        mapping: dict[str, str] = {}
        champion_rows = sorted(
            self.registry.by_id.items(),
            key=(
                (lambda item: item[0].casefold())
                if self.ui_language == "en"
                else (lambda item: item[1][1])
            ),
        )
        for champion_id, (_key, name_ko) in champion_rows:
            display = (
                champion_id if self.ui_language == "en"
                else name_ko
            )
            values.append(display)
            mapping[display] = champion_id
        self._build_champion_display_to_id = mapping
        if not hasattr(self, "build_champion_combo"):
            return
        self.build_champion_combo.configure(values=values)
        current = next(
            (
                display for display, champion_id in mapping.items()
                if champion_id == self._build_selected_champion_id
            ),
            values[0] if values else "",
        )
        self.build_champion_var.set(current)

    def _sync_build_selection_from_draft(
        self,
        draft: DraftSnapshot | None = None,
        *,
        render: bool = False,
    ) -> bool:
        """Make Apply Build follow the local HOVER/locked draft champion."""
        current_draft = draft or self.draft
        selection = local_draft_selection(current_draft)
        champion_id = (
            self.registry.normalize_id(selection.champion_id)
            if selection and selection.champion_id else self._build_selected_champion_id
        )
        if champion_id not in self.registry.by_id:
            return False
        role = current_draft.my_role
        guide_matches = bool(
            self.build_guide
            and self.build_guide.champion_id == champion_id
            and self.build_guide.position == role
        )
        selection_changed = champion_id != self._build_selected_champion_id
        if not selection_changed and guide_matches:
            self._refresh_build_champion_values()
            return False

        self._build_selected_champion_id = champion_id
        if not self.demo:
            self.storage.set_setting("build_selected_champion", champion_id)
            self.build_guide = self.storage.load_build_guide(champion_id, role)
        else:
            _key, name_ko = self.registry.by_id[champion_id]
            self.build_guide = replace(
                self._demo_build(),
                champion_id=champion_id,
                champion_name_ko=name_ko,
                position=role,
            )
        self._build_rune_index = 0
        self._build_spell_index = 0
        self._build_rune_manual = False
        self._reset_rune_editor()
        if self.build_guide:
            self._prefetch_build_assets(self.build_guide)
        self._build_render_signature = ""
        self._refresh_build_champion_values()
        if render:
            self._render_build()
        return True

    def _on_build_champion_selected(self, _event: tk.Event | None = None) -> None:
        champion_id = self._build_champion_display_to_id.get(
            self.build_champion_var.get(), ""
        )
        if not champion_id:
            return
        self._build_selected_champion_id = champion_id
        self.storage.set_setting("build_selected_champion", champion_id)
        self._build_rune_index = 0
        self._build_spell_index = 0
        self._build_rune_manual = False
        self.build_guide = self.storage.load_build_guide(champion_id, self.draft.my_role)
        if self.build_guide:
            self._prefetch_build_assets(self.build_guide)
        self._render_build()
        cache_due = self._build_cooldown_remaining(
            champion_id, self.draft.my_role
        ).total_seconds() <= 0
        request_key = (champion_id, self.draft.my_role)
        needs_statistics = bool(
            self.build_guide and not build_guide_has_statistics(self.build_guide)
        )
        statistics_due = (
            self._build_statistics_upgrade_remaining(
                champion_id, self.draft.my_role
            ).total_seconds() <= 0
        )
        if (
            not self.demo and not self._build_bulk_downloading
            and request_key not in self._tab_build_refresh_attempted
            and (
                not self.build_guide
                or (needs_statistics and statistics_due)
                or (not needs_statistics and cache_due)
            )
        ):
            self._tab_build_refresh_attempted.add(request_key)
            self._refresh_build_guide(automatic=True)

    def _refresh_build_guide(self, automatic: bool = False) -> None:
        if self.demo or self._build_refreshing or self._build_bulk_downloading:
            return
        champion_id = self._build_selected_champion_id
        position = self.draft.my_role
        if not champion_id:
            return
        remaining = self._build_cooldown_remaining(champion_id, position)
        needs_statistics = bool(
            self.build_guide and not build_guide_has_statistics(self.build_guide)
        )
        if needs_statistics:
            remaining = self._build_statistics_upgrade_remaining(
                champion_id, position
            )
        if (
            remaining.total_seconds() > 0
            and self.build_guide
        ):
            if not automatic:
                minutes = max(1, int(remaining.total_seconds() // 60) + 1)
                hours, minutes = divmod(minutes, 60)
                messagebox.showinfo(
                    "빌드 갱신 쿨타임",
                    f"이 챔피언 빌드는 약 {hours}시간 {minutes}분 뒤 다시 갱신할 수 있습니다.",
                    parent=self.root,
                )
            return
        if needs_statistics:
            # Persist the attempt before the request. A failed migration must
            # still obey the same configurable cooldown after an app restart.
            self._mark_build_statistics_upgrade_attempt(champion_id, position)
        signature = f"{position}:{champion_id}"
        self._build_request_signature = signature
        self._build_refreshing = True
        self.build_refresh_button.configure(
            state="disabled", text="빌드 불러오는 중..."
        )

        def success(guide: ChampionBuildGuide) -> None:
            self._build_refreshing = False
            self.storage.save_build_guide(guide)
            if signature == f"{self.draft.my_role}:{self._build_selected_champion_id}":
                self.build_guide = guide
                self._build_rune_index = 0
                self._build_spell_index = 0
                self._build_rune_manual = False
                self.build_status_label.configure(
                    text="빌드 데이터 저장 완료 · 아이콘을 로컬에 미리 받는 중...",
                    fg=COLORS["blue"],
                )
                self._prefetch_build_assets(
                    guide,
                    lambda: self.build_status_label.configure(
                        text="빌드와 룬·스펠·아이템 이미지를 로컬에 저장했습니다.",
                        fg=COLORS["green"],
                    ) if signature == (
                        f"{self.draft.my_role}:{self._build_selected_champion_id}"
                    ) else None,
                )
            self._render_build()

        def error(exc: Exception) -> None:
            self._build_refreshing = False
            self.build_status_label.configure(text=str(exc), fg=COLORS["red"])
            self.build_refresh_button.configure(
                state="normal", text="OP.GG 빌드 갱신"
            )
            if not automatic:
                messagebox.showerror("빌드 불러오기 실패", str(exc), parent=self.root)

        self._background(
            lambda: self.opgg_client.refresh_build(champion_id, position), success, error
        )

    def _toggle_bulk_build_download(
        self, on_complete: Callable[[bool], None] | None = None,
    ) -> None:
        if self.demo:
            return
        if self._build_bulk_downloading:
            if on_complete:
                on_complete(False)
                return
            self._build_bulk_cancel.set()
            self.build_bulk_button.configure(state="disabled", text="저장 중지 중...")
            self.build_bulk_status_label.configure(
                text="현재 파일 저장을 마친 뒤 중지합니다.", fg=COLORS["orange"]
            )
            return
        remaining = self._cache_job_cooldown_remaining("opgg_builds_all")
        if remaining.total_seconds() > 0:
            if on_complete:
                on_complete(True)
            else:
                messagebox.showinfo(
                    "전체 빌드 캐시",
                    self._cache_remaining_text(remaining),
                    parent=self.root,
                )
            return
        self._build_bulk_downloading = True
        self._build_bulk_cancel.clear()
        self.build_bulk_button.configure(text="전체 저장 중지", state="normal")
        self.build_refresh_button.configure(state="disabled")
        if not self.build_bulk_progress.winfo_manager():
            self.build_bulk_progress.pack(fill="x", pady=(8, 0))
        self.build_bulk_progress.configure(maximum=1, value=0)
        self.build_bulk_status_label.configure(
            text="OP.GG의 5개 포지션 챔피언 목록을 확인하는 중...", fg=COLORS["blue"]
        )
        positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")

        def update_progress(
            done: int, total: int, position: str,
            saved: int, cached: int, failed: int,
        ) -> None:
            self.build_bulk_progress.configure(maximum=max(total, 1), value=done)
            self.build_bulk_status_label.configure(
                text=(
                    f"{position_name(position)} · {done}/{total} · "
                    f"신규 {saved} · 기존 {cached} · 실패 {failed}"
                ),
                fg=COLORS["blue"],
            )

        def work() -> dict[str, int | bool]:
            catalogs: list[tuple[str, str, str]] = []
            catalog_failures = 0
            for position in positions:
                if self._build_bulk_cancel.is_set():
                    break
                try:
                    catalog = self.storage.load_opgg_position_catalog(
                        position,
                        max_age=self._request_max_age("opgg_build_cooldown_hours"),
                    )
                    if catalog:
                        patch, champion_ids = catalog
                    else:
                        patch, champion_ids = self.opgg_client.refresh_position_champions(
                            position
                        )
                        self.storage.save_opgg_position_catalog(
                            position, patch, champion_ids
                        )
                    catalogs.extend(
                        (position, patch, champion_id)
                        for champion_id in champion_ids
                    )
                except OpggError:
                    catalog_failures += 1
            if not catalogs and not self._build_bulk_cancel.is_set():
                raise OpggError("OP.GG에서 전체 챔피언 목록을 읽지 못했습니다.")
            catalogs = list(dict.fromkeys(catalogs))
            total = len(catalogs)
            saved = cached_count = failed = image_downloads = image_failures = 0
            self._post_ui(lambda: self.build_bulk_progress.configure(maximum=max(total, 1)))
            for done, (position, patch, champion_id) in enumerate(catalogs, start=1):
                if self._build_bulk_cancel.is_set():
                    break
                guide = self.storage.load_build_guide(champion_id, position)
                same_patch = bool(
                    guide and patch != "UNKNOWN" and guide.patch == patch
                    and build_guide_has_statistics(guide)
                )
                recently_cached = bool(
                    guide and patch == "UNKNOWN" and
                    build_guide_has_statistics(guide) and
                    self._build_cooldown_remaining(
                        champion_id, position
                    ).total_seconds() > 0
                )
                if same_patch or recently_cached:
                    cached_count += 1
                else:
                    try:
                        guide = self.opgg_client.refresh_build(champion_id, position)
                        self.storage.save_build_guide(guide)
                        saved += 1
                    except OpggError:
                        failed += 1
                        guide = None
                    time.sleep(0.7)
                if guide:
                    assets = self.build_asset_preloader.cache_guide(guide)
                    image_downloads += assets["downloaded"]
                    image_failures += assets["failed"]
                self._post_ui(
                    lambda current=done, count=total, role=position,
                           new=saved, old=cached_count, errors=failed:
                    update_progress(current, count, role, new, old, errors)
                )
            return {
                "total": total,
                "saved": saved,
                "cached": cached_count,
                "failed": failed + catalog_failures,
                "images": image_downloads,
                "image_failures": image_failures,
                "cancelled": self._build_bulk_cancel.is_set(),
            }

        def finish(result: dict[str, int | bool]) -> None:
            self._build_bulk_downloading = False
            self.build_bulk_button.configure(
                text="전체 챔피언 빌드 로컬 저장", state="normal"
            )
            self.build_refresh_button.configure(state="normal")
            cancelled = bool(result["cancelled"])
            if not cancelled:
                self.storage.mark_cache_job_success("opgg_builds_all")
            self.build_bulk_status_label.configure(
                text=(
                    f"{'중지됨' if cancelled else '전체 저장 완료'} · "
                    f"신규 {result['saved']} · 기존 {result['cached']} · "
                    f"빌드 실패 {result['failed']} · 이미지 {result['images']}개 다운로드 · "
                    f"이미지 실패 {result['image_failures']}"
                ),
                fg=COLORS["orange"] if cancelled else COLORS["green"],
            )
            cached_guide = self.storage.load_build_guide(
                self._build_selected_champion_id, self.draft.my_role
            )
            if cached_guide:
                self.build_guide = cached_guide
                self._build_rune_index = 0
                self._build_spell_index = 0
                self._prefetch_build_assets(cached_guide)
                self._render_build()
            self.root.after(3500, self.build_bulk_progress.pack_forget)
            if on_complete:
                on_complete(not cancelled)

        def error(exc: Exception) -> None:
            self._build_bulk_downloading = False
            self.build_bulk_button.configure(
                text="전체 챔피언 빌드 로컬 저장", state="normal"
            )
            self.build_refresh_button.configure(state="normal")
            self.build_bulk_status_label.configure(text=str(exc), fg=COLORS["red"])
            self.root.after(3500, self.build_bulk_progress.pack_forget)
            messagebox.showerror("전체 빌드 저장 실패", str(exc), parent=self.root)
            if on_complete:
                on_complete(False)

        self._background(work, finish, error)

    def _refresh_rune_catalog_background(self) -> None:
        if self._rune_catalog_refreshing:
            return
        self._rune_catalog_refreshing = True

        def success(count: int) -> None:
            self._rune_catalog_refreshing = False
            self._reset_rune_editor()
            if hasattr(self, "build_status_label"):
                self.build_status_label.configure(
                    text=self._text("build.rune_cache", count=count),
                    fg=COLORS["green"],
                )
            self._render_build()

        def error(exc: Exception) -> None:
            self._rune_catalog_refreshing = False
            if not self.rune_catalog.ready and hasattr(self, "build_status_label"):
                self.build_status_label.configure(
                    text=(
                        "롤 클라이언트를 실행하면 전체 룬 선택 데이터와 설명을 "
                        "로컬에 저장합니다."
                    ),
                    fg=COLORS["orange"],
                )
            self._render_build()

        self._background(
            lambda: self.rune_catalog.refresh_from_lcu(self.lcu), success, error
        )

    def _reset_rune_editor(self) -> None:
        self._rune_editor_source = ""
        self._rune_editor_custom = False
        self._rune_primary_style_id = 0
        self._rune_sub_style_id = 0
        self._rune_primary_perks = []
        self._rune_secondary_perks = {}
        self._rune_secondary_order = []
        self._rune_shards = []
        self._rune_choice_widgets = {}
        self._rune_editor_hint_label = None
        self._rune_editor_summary_label = None

    @staticmethod
    def _slot_default(style: RuneStyle, slot_index: int, default_index: int) -> int:
        if slot_index >= len(style.slots) or not style.slots[slot_index]:
            return 0
        preferred = (
            style.default_perks[default_index]
            if default_index < len(style.default_perks) else 0
        )
        return preferred if preferred in style.slots[slot_index] else style.slots[slot_index][0]

    def _set_default_secondary_perks(self) -> None:
        style = self.rune_catalog.style(self._rune_sub_style_id)
        self._rune_secondary_perks = {}
        self._rune_secondary_order = []
        if not style:
            return
        for slot_index in range(1, min(4, len(style.slots))):
            if not style.slots[slot_index]:
                continue
            self._rune_secondary_perks[slot_index] = style.slots[slot_index][0]
            self._rune_secondary_order.append(slot_index)
            if len(self._rune_secondary_order) == 2:
                break

    def _ensure_rune_editor(self, base: RuneBuild) -> None:
        if not self.rune_catalog.ready:
            return
        source = (
            f"{self._build_selected_champion_id}:{self.draft.my_role}:"
            f"{self._build_rune_index}:{base.primary_style_id}:{base.sub_style_id}:"
            + ",".join(str(perk.asset_id) for perk in base.perks)
        )
        if source == self._rune_editor_source:
            return
        primary = self.rune_catalog.style(base.primary_style_id)
        if not primary:
            primary = self.rune_catalog.style(
                self.rune_catalog.style_order[0] if self.rune_catalog.style_order else 0
            )
        if not primary:
            return
        sub = self.rune_catalog.style(base.sub_style_id)
        if not sub or sub.style_id == primary.style_id:
            sub = self.rune_catalog.style(
                next(
                    (
                        value for value in primary.allowed_sub_styles
                        if self.rune_catalog.style(value)
                    ),
                    0,
                )
            )
        if not sub:
            return
        base_ids = [int(perk.asset_id) for perk in base.perks]
        self._rune_editor_source = source
        self._rune_editor_custom = False
        self._rune_primary_style_id = primary.style_id
        self._rune_sub_style_id = sub.style_id
        self._rune_primary_perks = []
        for slot_index, slot in enumerate(primary.slots[:4]):
            selected = next((perk_id for perk_id in base_ids if perk_id in slot), 0)
            self._rune_primary_perks.append(
                selected or self._slot_default(primary, slot_index, slot_index)
            )
        self._rune_secondary_perks = {}
        self._rune_secondary_order = []
        for perk_id in base_ids:
            slot_index = self.rune_catalog.slot_index(
                sub.style_id, perk_id, include_shards=False
            )
            if slot_index is None or slot_index == 0 or slot_index in self._rune_secondary_perks:
                continue
            self._rune_secondary_perks[slot_index] = perk_id
            self._rune_secondary_order.append(slot_index)
            if len(self._rune_secondary_order) == 2:
                break
        if len(self._rune_secondary_order) < 2:
            existing = dict(self._rune_secondary_perks)
            order = list(self._rune_secondary_order)
            self._set_default_secondary_perks()
            for slot_index, perk_id in existing.items():
                self._rune_secondary_perks[slot_index] = perk_id
            self._rune_secondary_order = order + [
                slot_index for slot_index in self._rune_secondary_order
                if slot_index not in order
            ]
            self._rune_secondary_order = self._rune_secondary_order[:2]
            self._rune_secondary_perks = {
                slot_index: self._rune_secondary_perks[slot_index]
                for slot_index in self._rune_secondary_order
            }
        self._rune_shards = []
        for offset in range(3):
            slot_index = 4 + offset
            slot = primary.slots[slot_index] if slot_index < len(primary.slots) else []
            selected = next((perk_id for perk_id in base_ids if perk_id in slot), 0)
            self._rune_shards.append(
                selected or self._slot_default(primary, slot_index, 6 + offset)
            )

    def _set_rune_style(self, kind: str, style_id: int) -> None:
        style = self.rune_catalog.style(style_id)
        if not style:
            return
        self._rune_editor_custom = True
        if kind == "primary":
            self._rune_primary_style_id = style.style_id
            self._rune_primary_perks = [
                self._slot_default(style, slot_index, slot_index)
                for slot_index in range(min(4, len(style.slots)))
            ]
            if self._rune_sub_style_id not in style.allowed_sub_styles:
                self._rune_sub_style_id = next(
                    (
                        value for value in style.allowed_sub_styles
                        if self.rune_catalog.style(value)
                    ),
                    0,
                )
                self._set_default_secondary_perks()
            self._rune_shards = [
                self._slot_default(style, 4 + offset, 6 + offset)
                for offset in range(3)
            ]
        else:
            if style.style_id == self._rune_primary_style_id:
                return
            self._rune_sub_style_id = style.style_id
            self._set_default_secondary_perks()
        self._render_rune_panel_only()

    def _select_primary_rune(self, slot_index: int, perk_id: int) -> None:
        style = self.rune_catalog.style(self._rune_primary_style_id)
        if not style or slot_index >= len(style.slots) or perk_id not in style.slots[slot_index]:
            return
        while len(self._rune_primary_perks) <= slot_index:
            self._rune_primary_perks.append(0)
        self._rune_primary_perks[slot_index] = perk_id
        self._rune_editor_custom = True
        self._refresh_rune_editor_selection_state()

    def _select_secondary_rune(self, slot_index: int, perk_id: int) -> None:
        style = self.rune_catalog.style(self._rune_sub_style_id)
        if (
            not style or slot_index == 0 or slot_index >= min(4, len(style.slots))
            or perk_id not in style.slots[slot_index]
        ):
            return
        if slot_index in self._rune_secondary_perks:
            self._rune_secondary_perks[slot_index] = perk_id
        else:
            if len(self._rune_secondary_order) >= 2:
                removed = self._rune_secondary_order.pop(0)
                self._rune_secondary_perks.pop(removed, None)
            self._rune_secondary_order.append(slot_index)
            self._rune_secondary_perks[slot_index] = perk_id
        self._rune_editor_custom = True
        self._refresh_rune_editor_selection_state()

    def _select_rune_shard(self, row: int, perk_id: int) -> None:
        style = self.rune_catalog.style(self._rune_primary_style_id)
        slot_index = 4 + row
        if not style or slot_index >= len(style.slots) or perk_id not in style.slots[slot_index]:
            return
        while len(self._rune_shards) <= row:
            self._rune_shards.append(0)
        self._rune_shards[row] = perk_id
        self._rune_editor_custom = True
        self._refresh_rune_editor_selection_state()

    def _active_rune_build(self, base: RuneBuild) -> RuneBuild:
        self._ensure_rune_editor(base)
        if not self.rune_catalog.ready or len(self._rune_primary_perks) != 4:
            return base
        secondary = [
            self._rune_secondary_perks[slot_index]
            for slot_index in sorted(self._rune_secondary_perks)
        ][:2]
        perk_ids = [*self._rune_primary_perks, *secondary, *self._rune_shards]
        if len(perk_ids) != 9 or not all(perk_ids):
            return base
        return RuneBuild(
            name=f"{base.name} · {'직접 편집' if self._rune_editor_custom else '추천'}",
            primary_style_id=self._rune_primary_style_id,
            sub_style_id=self._rune_sub_style_id,
            perks=[self.rune_catalog.asset(perk_id) for perk_id in perk_ids],
        )

    def _current_base_rune_build(self) -> RuneBuild | None:
        if not self.build_guide or not self.build_guide.rune_builds:
            return None
        return self.build_guide.rune_builds[
            min(self._build_rune_index, len(self.build_guide.rune_builds) - 1)
        ]

    def _refresh_rune_editor_selection_state(self) -> None:
        """Update only rune highlights/text; do not rebuild the build tab."""
        for (kind, slot_index, perk_id), (widget, accent) in list(
            self._rune_choice_widgets.items()
        ):
            try:
                if not widget.winfo_exists():
                    continue
                selected = (
                    kind == "primary"
                    and slot_index < len(self._rune_primary_perks)
                    and self._rune_primary_perks[slot_index] == perk_id
                ) or (
                    kind == "secondary"
                    and self._rune_secondary_perks.get(slot_index) == perk_id
                ) or (
                    kind == "shard"
                    and slot_index < len(self._rune_shards)
                    and self._rune_shards[slot_index] == perk_id
                )
                widget.configure(
                    bg="#2c2144" if selected else "#0b1220",
                    fg=COLORS["text"] if selected else COLORS["muted"],
                    activebackground="#3b2c59" if selected else COLORS["chip"],
                    highlightthickness=2,
                    highlightbackground=accent if selected else COLORS["border"],
                )
            except tk.TclError:
                continue
        try:
            if self._rune_editor_hint_label and self._rune_editor_hint_label.winfo_exists():
                self._rune_editor_hint_label.configure(
                    fg=COLORS["green"] if self._rune_editor_custom else COLORS["muted"]
                )
            base = self._current_base_rune_build()
            if (
                base and self._rune_editor_summary_label
                and self._rune_editor_summary_label.winfo_exists()
            ):
                active = self._active_rune_build(base)
                names = " · ".join(
                    self._rune_name_text(perk.asset_id, perk.name)
                    for perk in active.perks
                )
                self._rune_editor_summary_label.configure(
                    text=(
                        self._tr(
                            "직접 편집 적용값"
                            if self._rune_editor_custom else "추천 적용값"
                        )
                    ) + f"\n{names}"
                )
        except tk.TclError:
            return
        self._mark_build_render_current()

    def _render_rune_panel_only(self) -> None:
        """Rebuild only the rune tree when primary/secondary styles change."""
        base = self._current_base_rune_build()
        if not base or not hasattr(self, "build_runes_frame"):
            return
        old_button = self._rune_apply_button
        if old_button in self.build_apply_buttons:
            self.build_apply_buttons.remove(old_button)
        self._clear(self.build_runes_frame)
        self._rune_choice_widgets = {}
        self._rune_editor_hint_label = None
        self._rune_editor_summary_label = None
        self._render_rune_editor(base)
        self._mark_build_render_current()

    def _build_state_signature(self) -> str:
        return repr((
            self._build_selected_champion_id,
            self.draft.my_role,
            self.draft.selected_enemy_support_id,
            self.build_guide,
            self._build_rune_index,
            self._build_spell_index,
            self._build_rune_manual,
            self._flash_slot,
            self._build_item_details_expanded,
            self._rune_editor_source,
            self._rune_editor_custom,
            self._rune_primary_style_id,
            self._rune_sub_style_id,
            tuple(self._rune_primary_perks),
            tuple(sorted(self._rune_secondary_perks.items())),
            tuple(self._rune_secondary_order),
            tuple(self._rune_shards),
            self.rune_catalog.updated_at,
            self._build_refreshing,
            self._build_applying,
            self.demo,
        ))

    def _mark_build_render_current(self) -> None:
        self._build_render_signature = self._build_state_signature()

    def _update_build_preset_selection(self) -> None:
        for card, preset, icon_strip, index in self._build_preset_widgets:
            try:
                if not card.winfo_exists():
                    continue
                selected = index == self._build_rune_index
                background = "#4b3470" if selected else COLORS["chip"]
                card.configure(
                    bg=background,
                    highlightbackground=(
                        COLORS["purple"] if selected else COLORS["border"]
                    ),
                )
                icon_strip.configure(bg=background)
                for child in icon_strip.winfo_children():
                    child.configure(bg=background)
                    setattr(child, "_advisor_base_bg", background)
                preset.configure(
                    bg=background,
                    fg=COLORS["text"] if selected else COLORS["muted"],
                    activebackground="#5b4184" if selected else "#263a59",
                )
            except tk.TclError:
                continue

    def _set_build_rune_index(self, index: int) -> None:
        if self.build_guide and 0 <= index < len(self.build_guide.rune_builds):
            self._build_rune_index = index
            self._build_rune_manual = True
            self._reset_rune_editor()
            self._update_build_preset_selection()
            self._render_rune_panel_only()

    def _select_matchup_rune(self) -> None:
        if not self.build_guide or not self.build_guide.rune_builds:
            return
        self._build_rune_index = matchup_rune_index(
            self.build_guide.rune_builds,
            self.draft.selected_enemy_support_id,
        )
        self._build_rune_manual = False
        self._reset_rune_editor()
        self._update_build_preset_selection()
        self._render_rune_panel_only()

    def _set_flash_slot(self, slot: str) -> None:
        normalized = str(slot).upper()
        if normalized not in {"D", "F"} or normalized == self._flash_slot:
            return
        self._flash_slot = normalized
        self.storage.set_setting("flash_slot", normalized)
        if (
            self.build_guide and self._build_spell_row
            and self._build_spell_row.winfo_exists()
        ):
            self._render_spell_assets_row(
                self._build_spell_row, self.build_guide, clear=True
            )
        for value, button in self._flash_slot_buttons.items():
            selected = value == self._flash_slot
            background = "#214a6b" if selected else COLORS["chip"]
            button.configure(
                bg=background,
                fg=COLORS["blue"] if selected else COLORS["muted"],
                highlightbackground=(
                    COLORS["blue"] if selected else COLORS["border"]
                ),
            )
            setattr(button, "_advisor_base_bg", background)
        self._mark_build_render_current()

    @staticmethod
    def _spell_builds(guide: ChampionBuildGuide) -> list[SummonerSpellBuild]:
        builds = list(guide.summoner_spell_builds[:3])
        if builds:
            return builds
        if guide.summoner_spells:
            return [SummonerSpellBuild(
                name="추천 스펠 1",
                spells=list(guide.summoner_spells[:2]),
            )]
        return []

    def _selected_spell_build(
        self, guide: ChampionBuildGuide,
    ) -> SummonerSpellBuild | None:
        builds = self._spell_builds(guide)
        if not builds:
            return None
        self._build_spell_index = min(
            max(int(self._build_spell_index), 0), len(builds) - 1
        )
        return builds[self._build_spell_index]

    def _update_build_spell_selection(self) -> None:
        for card, label, icon_strip, index in self._build_spell_choice_widgets:
            try:
                if not card.winfo_exists():
                    continue
                selected = index == self._build_spell_index
                background = "#214a6b" if selected else COLORS["chip"]
                card.configure(
                    bg=background,
                    highlightbackground=(
                        COLORS["blue"] if selected else COLORS["border"]
                    ),
                )
                label.configure(
                    bg=background,
                    fg=COLORS["text"] if selected else COLORS["muted"],
                    activebackground="#2b5f88" if selected else "#263a59",
                )
                icon_strip.configure(bg=background)
                for child in icon_strip.winfo_children():
                    child.configure(bg=background)
                    setattr(child, "_advisor_base_bg", background)
            except tk.TclError:
                continue

    def _set_build_spell_index(self, index: int) -> None:
        if not self.build_guide:
            return
        builds = self._spell_builds(self.build_guide)
        if not (0 <= index < len(builds)):
            return
        self._build_spell_index = index
        self._update_build_spell_selection()
        if self._build_spell_row and self._build_spell_row.winfo_exists():
            self._render_spell_assets_row(
                self._build_spell_row, self.build_guide, clear=True
            )
        self._mark_build_render_current()

    def _render_spell_build_choices(
        self, parent: tk.Widget, guide: ChampionBuildGuide,
    ) -> None:
        builds = self._spell_builds(guide)
        if not builds:
            tk.Label(
                parent, text=self._tr("추천 스펠 조합 없음"), bg=COLORS["panel_2"],
                fg=COLORS["muted"], font=("Malgun Gothic", 8),
            ).pack(anchor="w", pady=(6, 8))
            return
        self._build_spell_index = min(
            max(int(self._build_spell_index), 0), len(builds) - 1
        )
        tk.Label(
            parent, text=(
                f"OP.GG recommended combinations · {len(builds)}"
                if self.ui_language == "en"
                else f"OP.GG 추천 조합 · {len(builds)}개"
            ),
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w", pady=(4, 5))
        for index, spell_build in enumerate(builds):
            selected = index == self._build_spell_index
            background = "#214a6b" if selected else COLORS["chip"]
            choose = lambda selected_index=index: self._set_build_spell_index(
                selected_index
            )
            card = tk.Frame(
                parent, bg=background, padx=5, pady=5,
                highlightthickness=1,
                highlightbackground=COLORS["blue"] if selected else COLORS["border"],
            )
            card.pack(fill="x", pady=(0, 5))
            icon_strip = tk.Frame(card, bg=background)
            icon_strip.pack(side="left", padx=(0, 6))
            for spell in spell_build.spells[:2]:
                self._build_inline_asset_icon(
                    icon_strip, spell, "spell", choose,
                    size=28, background=background,
                ).pack(side="left", padx=1)
            label = tk.Button(
                card,
                text=(
                    f"{'Combination' if self.ui_language == 'en' else '조합'} {index + 1}\n"
                    f"{self._build_stat_text(spell_build.games, spell_build.win_rate)}"
                ),
                command=choose,
                bg=background,
                fg=COLORS["text"] if selected else COLORS["muted"],
                activebackground="#2b5f88" if selected else "#263a59",
                activeforeground=COLORS["text"],
                relief="flat", bd=0, cursor="hand2",
                anchor="w", justify="left", padx=3, pady=1,
                font=("Malgun Gothic", 7, "bold"),
            )
            label.pack(side="left", fill="both", expand=True)
            self._build_spell_choice_widgets.append(
                (card, label, icon_strip, index)
            )

    def _ordered_spell_assets(self, guide: ChampionBuildGuide) -> list[BuildAsset]:
        selected = self._selected_spell_build(guide)
        spells = list(
            selected.spells[:2] if selected else guide.summoner_spells[:2]
        )
        preferred_index = 1 if self._flash_slot == "F" else 0
        flash_index = next(
            (index for index, spell in enumerate(spells) if int(spell.asset_id) == 4),
            None,
        )
        if flash_index is not None and flash_index != preferred_index:
            spells.reverse()
        return spells

    def _render_spell_assets_row(
        self, row: tk.Frame, guide: ChampionBuildGuide, *, clear: bool = False,
    ) -> None:
        if clear:
            self._clear(row)
        for slot, spell in zip(("D", "F"), self._ordered_spell_assets(guide)):
            spell_card = tk.Frame(row, bg=COLORS["surface"], padx=3, pady=3)
            spell_card.pack(side="left", fill="x", expand=True, padx=3)
            tk.Label(
                spell_card, text=slot, bg=COLORS["chip"], fg=COLORS["gold"],
                font=("Consolas", 9, "bold"), padx=5, pady=2,
            ).pack(anchor="nw")
            self._build_asset_widget(spell_card, spell, "spell", 42).pack(fill="x")

    def _render_skill_grid(
        self, parent: tk.Widget, guide: ChampionBuildGuide
    ) -> None:
        sequence = list(guide.skill_sequence[:18])
        if not sequence:
            tk.Label(
                parent, text="레벨별 데이터 없음", bg=COLORS["panel_2"],
                fg=COLORS["muted"], font=("Malgun Gothic", 8),
            ).pack(anchor="w")
            return
        grid = tk.Frame(parent, bg=COLORS["panel_2"])
        grid.pack(fill="x", pady=(4, 0))
        tk.Label(
            grid, text="", width=2, bg=COLORS["panel_2"]
        ).grid(row=0, column=0)
        for level in range(1, 19):
            tk.Label(
                grid, text=str(level), width=2, bg=COLORS["panel_2"],
                fg=COLORS["muted"], font=("Consolas", 6),
            ).grid(row=0, column=level, padx=1, pady=1)
        ability_colors = {
            "Q": COLORS["red"], "W": COLORS["blue"],
            "E": COLORS["green"], "R": COLORS["gold"],
        }
        for row, ability in enumerate(("Q", "W", "E", "R"), start=1):
            tk.Label(
                grid, text=ability, width=2, bg=COLORS["panel_2"],
                fg=ability_colors[ability], font=("Consolas", 8, "bold"),
            ).grid(row=row, column=0, padx=(0, 2), pady=1)
            for level in range(1, 19):
                selected = level <= len(sequence) and sequence[level - 1] == ability
                tk.Label(
                    grid, text=ability if selected else "", width=2,
                    bg=ability_colors[ability] if selected else COLORS["surface"],
                    fg="#07101a" if selected else COLORS["muted"],
                    font=("Consolas", 6, "bold"),
                ).grid(row=row, column=level, padx=1, pady=1)

    def _prefetch_build_assets(
        self, guide: ChampionBuildGuide,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        """Warm the disk/image caches once, then notify the UI only once."""
        entries: list[tuple[str, BuildAsset, int]] = []
        seen: set[tuple[str, int, str]] = set()
        for rune_build in guide.rune_builds:
            for perk in rune_build.perks:
                key = ("rune", perk.asset_id, perk.icon_url)
                if key not in seen:
                    seen.add(key)
                    entries.append(("rune", perk, 34))
        spell_assets = (
            spell
            for spell_build in self._spell_builds(guide)
            for spell in spell_build.spells
        )
        for spell in spell_assets:
            key = ("spell", spell.asset_id, "")
            if key not in seen:
                seen.add(key)
                entries.append(("spell", spell, 42))
        for group in guide.item_groups:
            for item in group.items:
                key = ("item", item.asset_id, "")
                if key not in seen:
                    seen.add(key)
                    entries.append(("item", item, 30))
        if not entries:
            if on_complete:
                on_complete()
            return
        remaining = len(entries)

        def ready() -> None:
            nonlocal remaining
            remaining -= 1
            if remaining == 0 and on_complete:
                on_complete()

        for kind, asset, size in entries:
            if kind == "item":
                image = self.item_icon_cache.get(asset.asset_id, size, ready)
            else:
                image = self.build_icon_cache.get(
                    f"{kind}:{asset.asset_id}", asset.icon_url, size, ready
                )
            if image:
                ready()

    def _build_inline_asset_icon(
        self, parent: tk.Widget, asset: BuildAsset, kind: str,
        command: Callable[[], None] | None = None, size: int = 28,
        background: str = COLORS["surface"],
    ) -> tk.Widget:
        widget_class = tk.Button if command else tk.Label
        options: dict[str, object] = {
            "text": "·",
            "bg": background,
            "fg": COLORS["muted"],
            "font": ("Malgun Gothic", 12, "bold"),
            "padx": 2,
            "pady": 2,
            "highlightthickness": 1,
            "highlightbackground": COLORS["border"],
        }
        if command:
            options.update({
                "command": command,
                "cursor": "hand2",
                "relief": "flat",
                "bd": 0,
                "activebackground": COLORS["chip"],
            })
        icon = widget_class(parent, **options)

        def load_image() -> None:
            try:
                if not icon.winfo_exists():
                    return
                if kind == "item":
                    ready_image = self.item_icon_cache.get(asset.asset_id, size)
                else:
                    ready_image = self.build_icon_cache.get(
                        f"{kind}:{asset.asset_id}", asset.icon_url, size
                    )
                if ready_image:
                    icon.configure(image=ready_image, text="")
                    icon.image = ready_image
            except tk.TclError:
                return

        if kind == "item":
            image = self.item_icon_cache.get(asset.asset_id, size, load_image)
            tooltip = lambda value=asset: self._asset_tooltip_text(value, "item")
        else:
            image = self.build_icon_cache.get(
                f"{kind}:{asset.asset_id}", asset.icon_url, size, load_image
            )
            if kind == "rune":
                tooltip = lambda value=asset: self._asset_tooltip_text(value, "rune")
            else:
                tooltip = lambda value=asset: self._asset_tooltip_text(value, "spell")
        if image:
            icon.configure(image=image, text="")
            icon.image = image
        helper = _HoverTooltip(icon, tooltip)
        setattr(icon, "_advisor_tooltip", helper)
        return icon

    def _build_asset_widget(
        self, parent: tk.Widget, asset: BuildAsset, kind: str, size: int = 34,
        compact: bool = False,
    ) -> tk.Frame:
        frame = tk.Frame(
            parent, bg=COLORS["surface"],
            padx=2 if compact else 5, pady=3 if compact else 5,
        )
        icon = tk.Label(
            frame, text="·", bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Malgun Gothic", 16, "bold"),
        )
        icon.pack()

        def load_image() -> None:
            try:
                if not icon.winfo_exists():
                    return
                if kind == "item":
                    ready_image = self.item_icon_cache.get(asset.asset_id, size)
                else:
                    ready_image = self.build_icon_cache.get(
                        f"{kind}:{asset.asset_id}", asset.icon_url, size
                    )
                if ready_image:
                    icon.configure(image=ready_image, text="")
                    icon.image = ready_image
            except tk.TclError:
                return

        if kind == "item":
            image = self.item_icon_cache.get(asset.asset_id, size, load_image)
        else:
            image = self.build_icon_cache.get(
                f"{kind}:{asset.asset_id}", asset.icon_url, size, load_image
            )
        if image:
            icon.configure(image=image, text="")
            icon.image = image
        name_label = tk.Label(
            frame, text=self._asset_name_text(asset, kind), bg=COLORS["surface"], fg=COLORS["text"],
            wraplength=54 if compact else 92, justify="center",
            font=("Malgun Gothic", 6 if compact else 7),
        )
        name_label.pack(pady=(3, 0))
        if kind == "item":
            def update_item_name() -> None:
                try:
                    if name_label.winfo_exists():
                        name_label.configure(text=self._asset_name_text(asset, "item"))
                except tk.TclError:
                    return

            name_label.configure(
                text=self.item_icon_cache.localized_item_name(
                    asset.asset_id, self.ui_language, asset.name,
                    update_item_name,
                )
            )
        if kind == "item":
            tooltip = lambda value=asset: self._asset_tooltip_text(value, "item")
        elif kind == "rune":
            tooltip = lambda value=asset: self._asset_tooltip_text(value, "rune")
        else:
            tooltip = lambda value=asset: self._asset_tooltip_text(value, "spell")
        for target in (frame, icon, name_label):
            helper = _HoverTooltip(target, tooltip)
            setattr(target, "_advisor_tooltip", helper)
        return frame

    def _rune_choice_button(
        self, parent: tk.Widget, perk_id: int, selected: bool,
        command: Callable[[], None], accent: str, size: int = 30,
    ) -> tk.Button:
        option = self.rune_catalog.perk(perk_id)
        asset = self.rune_catalog.asset(perk_id)
        background = "#2c2144" if selected else "#0b1220"
        button = tk.Button(
            parent,
            text="·" if option else "?",
            command=command,
            bg=background,
            fg=COLORS["text"] if selected else COLORS["muted"],
            activebackground="#3b2c59" if selected else COLORS["chip"],
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=3,
            pady=3,
            cursor="hand2",
            highlightthickness=2,
            highlightbackground=accent if selected else COLORS["border"],
            highlightcolor=accent,
            font=("Malgun Gothic", 10, "bold"),
        )

        def load_image() -> None:
            try:
                if not button.winfo_exists():
                    return
                ready = self.build_icon_cache.get(
                    f"rune:{perk_id}", asset.icon_url, size
                )
                if ready:
                    button.configure(image=ready, text="")
                    button.image = ready
            except tk.TclError:
                return

        image = self.build_icon_cache.get(
            f"rune:{perk_id}", asset.icon_url, size, load_image
        )
        if image:
            button.configure(image=image, text="")
            button.image = image
        tooltip = _HoverTooltip(
            button,
            lambda value=perk_id, name=asset.name:
            (
                f"{self._rune_name_text(value, name)}\nRune ID {value}"
                if self.ui_language == "en"
                else self.rune_catalog.tooltip_text(value, name)
            ),
        )
        setattr(button, "_advisor_tooltip", tooltip)
        return button

    def _render_rune_editor(self, base: RuneBuild) -> None:
        self._rune_choice_widgets = {}
        self._rune_editor_hint_label = None
        self._rune_editor_summary_label = None
        tk.Label(
            self.build_runes_frame, text=self._tr("룬 · 직접 선택"),
            bg=COLORS["panel_2"], fg=COLORS["purple"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            self.build_runes_frame,
            text=(
                (
                    "Selected OP.GG source · " if self.ui_language == "en"
                    else "선택한 OP.GG 추천 원본 · "
                )
                + self._build_stat_text(base.games, base.win_rate)
            ),
            bg=COLORS["panel_2"],
            fg=(COLORS["green"] if base.win_rate is not None else COLORS["muted"]),
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w", pady=(3, 2))
        if not self.rune_catalog.ready:
            tk.Label(
                self.build_runes_frame,
                text=(
                    "Recommended runes can be applied immediately. "
                    "The full rune tree is loaded from the League Client and cached locally."
                    if self.ui_language == "en" else
                    "추천 룬은 바로 적용할 수 있습니다. 전체 룬 선택과 한글 설명은 "
                    "롤 클라이언트에서 읽어 로컬에 저장합니다."
                ),
                bg=COLORS["panel_2"], fg=COLORS["orange"],
                wraplength=390, justify="left", font=("Malgun Gothic", 7),
            ).pack(anchor="w", pady=(5, 7))
            rune_grid = tk.Frame(self.build_runes_frame, bg=COLORS["panel_2"])
            rune_grid.pack(fill="x")
            for index, perk in enumerate(base.perks):
                self._build_asset_widget(rune_grid, perk, "rune", 32).grid(
                    row=index // 3, column=index % 3,
                    sticky="nsew", padx=2, pady=2,
                )
            for column in range(3):
                rune_grid.grid_columnconfigure(column, weight=1)
            reload_button = self._button(
                self.build_runes_frame,
                "전체 룬 데이터 다시 읽기",
                self._refresh_rune_catalog_background,
                COLORS["orange"],
                width=20,
            )
            reload_button.pack(fill="x", pady=(8, 0))
        else:
            self._ensure_rune_editor(base)
            primary = self.rune_catalog.style(self._rune_primary_style_id)
            sub = self.rune_catalog.style(self._rune_sub_style_id)
            if primary and sub:
                self._rune_editor_hint_label = tk.Label(
                    self.build_runes_frame,
                    text=self._tr(
                        "추천값에서 시작 · 아이콘을 눌러 변경 · 보조 룬은 서로 다른 두 줄 선택"
                    ),
                    bg=COLORS["panel_2"],
                    fg=COLORS["green"] if self._rune_editor_custom else COLORS["muted"],
                    font=("Malgun Gothic", 7, "bold"),
                )
                self._rune_editor_hint_label.pack(anchor="w", pady=(3, 7))
                selectors = tk.Frame(self.build_runes_frame, bg=COLORS["panel_2"])
                selectors.pack(fill="x", pady=(0, 8))

                def style_selector(
                    title: str, style_ids: list[int], current_id: int,
                    kind: str,
                ) -> None:
                    card = tk.Frame(selectors, bg=COLORS["panel_2"])
                    card.pack(side="left", fill="x", expand=True, padx=(0, 4))
                    tk.Label(
                        card, text=self._tr(title), bg=COLORS["panel_2"], fg=COLORS["muted"],
                        font=("Malgun Gothic", 7, "bold"),
                    ).pack(anchor="w")
                    names = [
                        self._rune_style_text(
                            value, self.rune_catalog.styles[value].name,
                        )
                        for value in style_ids if value in self.rune_catalog.styles
                    ]
                    mapping = {
                        self._rune_style_text(
                            value, self.rune_catalog.styles[value].name,
                        ): value
                        for value in style_ids if value in self.rune_catalog.styles
                    }
                    variable = tk.StringVar(
                        value=(
                            self._rune_style_text(
                                current_id, self.rune_catalog.style(current_id).name,
                            ) if self.rune_catalog.style(current_id) else ""
                        )
                    )
                    combo = ttk.Combobox(
                        card, textvariable=variable, values=names,
                        state="readonly", width=9, font=("Malgun Gothic", 8),
                        style="Advisor.TCombobox",
                    )
                    combo.pack(fill="x", pady=(2, 0), ipady=2)
                    combo.bind(
                        "<<ComboboxSelected>>",
                        lambda _event, values=mapping, var=variable, target=kind:
                        self._set_rune_style(target, values.get(var.get(), 0)),
                    )
                    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                        combo.bind(sequence, self._on_rune_style_mousewheel)

                style_selector(
                    "주 룬 계열", list(self.rune_catalog.style_order),
                    primary.style_id, "primary",
                )
                style_selector(
                    "보조 룬 계열", list(primary.allowed_sub_styles),
                    sub.style_id, "secondary",
                )

                tree_area = tk.Frame(self.build_runes_frame, bg=COLORS["panel_2"])
                tree_area.pack(fill="x")
                primary_tree = tk.Frame(
                    tree_area, bg="#11182a", padx=5, pady=6,
                    highlightthickness=1, highlightbackground="#45355f",
                )
                primary_tree.pack(side="left", fill="both", expand=True, padx=(0, 3))
                secondary_tree = tk.Frame(
                    tree_area, bg="#101b27", padx=5, pady=6,
                    highlightthickness=1, highlightbackground="#28445e",
                )
                secondary_tree.pack(side="left", fill="both", expand=True, padx=(3, 0))
                tk.Label(
                    primary_tree, text=self._text(
                        "build.primary_tree",
                        style=self._rune_style_text(primary.style_id, primary.name),
                    ),
                    bg="#11182a", fg=COLORS["purple"],
                    font=("Malgun Gothic", 8, "bold"),
                ).pack(anchor="w", pady=(0, 4))
                tk.Label(
                    secondary_tree, text=self._text(
                        "build.secondary_tree",
                        style=self._rune_style_text(sub.style_id, sub.name),
                    ),
                    bg="#101b27", fg=COLORS["blue"],
                    font=("Malgun Gothic", 8, "bold"),
                ).pack(anchor="w", pady=(0, 4))
                for slot_index, slot in enumerate(primary.slots[:4]):
                    row = tk.Frame(primary_tree, bg="#11182a")
                    row.pack(fill="x", pady=2)
                    for perk_id in slot:
                        choice = self._rune_choice_button(
                            row, perk_id,
                            slot_index < len(self._rune_primary_perks)
                            and self._rune_primary_perks[slot_index] == perk_id,
                            lambda row_index=slot_index, value=perk_id:
                            self._select_primary_rune(row_index, value),
                            COLORS["purple"], 30,
                        )
                        self._rune_choice_widgets[(
                            "primary", slot_index, perk_id
                        )] = (choice, COLORS["purple"])
                        choice.pack(side="left", expand=True, padx=1)
                for slot_index, slot in enumerate(sub.slots[1:4], start=1):
                    row = tk.Frame(secondary_tree, bg="#101b27")
                    row.pack(fill="x", pady=2)
                    for perk_id in slot:
                        choice = self._rune_choice_button(
                            row, perk_id,
                            self._rune_secondary_perks.get(slot_index) == perk_id,
                            lambda row_index=slot_index, value=perk_id:
                            self._select_secondary_rune(row_index, value),
                            COLORS["blue"], 30,
                        )
                        self._rune_choice_widgets[(
                            "secondary", slot_index, perk_id
                        )] = (choice, COLORS["blue"])
                        choice.pack(side="left", expand=True, padx=1)

                shard_card = tk.Frame(
                    self.build_runes_frame, bg=COLORS["surface"], padx=6, pady=5,
                    highlightthickness=1, highlightbackground=COLORS["border"],
                )
                shard_card.pack(fill="x", pady=(7, 0))
                tk.Label(
                    shard_card, text=self._tr("능력치 파편 · 3줄에서 각각 1개"),
                    bg=COLORS["surface"], fg=COLORS["gold"],
                    font=("Malgun Gothic", 7, "bold"),
                ).pack(anchor="w", pady=(0, 3))
                shard_rows = tk.Frame(shard_card, bg=COLORS["surface"])
                shard_rows.pack(fill="x")
                shard_labels = ("공격", "유연", "방어")
                for offset, slot in enumerate(primary.slots[4:7]):
                    row = tk.Frame(shard_rows, bg=COLORS["surface"])
                    row.pack(fill="x", pady=1)
                    tk.Label(
                        row, text=self._tr(shard_labels[offset]), width=6,
                        bg=COLORS["surface"], fg=COLORS["muted"],
                        font=("Malgun Gothic", 7, "bold"), anchor="w",
                    ).pack(side="left", padx=(0, 3))
                    for perk_id in slot:
                        choice = self._rune_choice_button(
                            row, perk_id,
                            offset < len(self._rune_shards)
                            and self._rune_shards[offset] == perk_id,
                            lambda row_index=offset, value=perk_id:
                            self._select_rune_shard(row_index, value),
                            COLORS["gold"], 24,
                        )
                        self._rune_choice_widgets[(
                            "shard", offset, perk_id
                        )] = (choice, COLORS["gold"])
                        choice.pack(side="left", padx=2)

                active = self._active_rune_build(base)
                names = " · ".join(
                    self._rune_name_text(perk.asset_id, perk.name)
                    for perk in active.perks
                )
                self._rune_editor_summary_label = tk.Label(
                    self.build_runes_frame,
                    text=self._tr(
                        "직접 편집 적용값" if self._rune_editor_custom else "추천 적용값"
                    )
                    + f"\n{names}",
                    bg=COLORS["panel_2"], fg=COLORS["text"],
                    width=1, wraplength=440, justify="left", anchor="w",
                    font=("Malgun Gothic", 7),
                )
                self._rune_editor_summary_label.pack(fill="x", pady=(7, 0))
        rune_apply = self._button(
            self.build_runes_frame, "현재 선택 룬 적용",
            lambda: self._apply_build_component("runes"),
            COLORS["purple"], width=18,
        )
        rune_apply.configure(
            state="disabled" if self._build_applying or self.demo else "normal"
        )
        rune_apply.pack(fill="x", pady=(10, 0))
        self._rune_apply_button = rune_apply
        self.build_apply_buttons.append(rune_apply)

    def _render_build(self) -> None:
        if not hasattr(self, "build_status_label"):
            return
        if self._current_main_tab_index() != 3:
            return
        incoming_signature = self._build_state_signature()
        if incoming_signature == self._build_render_signature:
            return
        self._build_render_signature = incoming_signature
        role_name = self._position_text(self.draft.my_role)
        self.build_position_label.configure(
            text=self._text("build.position", role=role_name)
        )
        self.build_refresh_button.configure(
            state=(
                "disabled"
                if self._build_refreshing or self._build_bulk_downloading or self.demo
                else "normal"
            ),
            text=(
                self._tr("빌드 불러오는 중...") if self._build_refreshing
                else self._tr("OP.GG 빌드 갱신")
            ),
        )
        for frame in (
            self.build_presets_frame, self.build_runes_frame,
            self.build_spells_frame, self.build_items_frame,
        ):
            self._clear(frame)
        self._build_preset_widgets = []
        self._build_spell_choice_widgets = []
        self._build_spell_row = None
        self._flash_slot_buttons = {}
        self._build_item_details_frame = None
        self._build_item_details_toggle = None
        self._build_item_apply_button = None
        self.build_apply_buttons = list(self.build_quick_apply_buttons)
        guide = self.build_guide
        guide_matches = bool(
            guide and guide.champion_id == self._build_selected_champion_id
            and guide.position == self.draft.my_role and guide.rune_builds
        )
        if not guide_matches:
            self.build_guide_summary.configure(
                text=self._text(
                    "build.missing",
                    champion=self._champion_text(self._build_selected_champion_id),
                    role=role_name,
                )
            )
            for frame, text in (
                (self.build_presets_frame, "추천 목록 없음"),
                (self.build_runes_frame, "룬 데이터 없음"),
                (self.build_spells_frame, "스펠·스킬 데이터 없음"),
                (self.build_items_frame, "아이템 데이터 없음"),
            ):
                tk.Label(
                    frame, text=self._tr(text), bg=COLORS["panel_2"], fg=COLORS["muted"],
                    font=("Malgun Gothic", 9),
                ).pack(anchor="w")
            for button in self.build_apply_buttons:
                button.configure(state="disabled")
            self._mark_build_render_current()
            return
        assert guide is not None
        matchup_signature = (
            f"{guide.position}:{guide.champion_id}:"
            f"{self.draft.selected_enemy_support_id or 'UNKNOWN'}"
        )
        if matchup_signature != self._build_matchup_signature:
            self._build_matchup_signature = matchup_signature
            self._build_rune_manual = False
            self._reset_rune_editor()
        matchup_index = matchup_rune_index(
            guide.rune_builds, self.draft.selected_enemy_support_id
        )
        if not self._build_rune_manual:
            self._build_rune_index = matchup_index
        stamp = guide.updated_at.replace("T", " ")[:16]
        self.build_guide_summary.configure(
            text=self._text(
                "build.summary",
                champion=self._champion_text(
                    guide.champion_id, guide.champion_name_ko,
                ),
                role=role_name, patch=guide.patch, tier=guide.tier,
                stamp=stamp,
            ),
            fg=COLORS["muted"],
        )
        tk.Label(
            self.build_presets_frame, text=self._tr("추천 빌드 목록"),
            bg=COLORS["panel_2"], fg=COLORS["gold"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            self.build_presets_frame,
            text=self._tr("기본 추천과 상대 대응 추천을 구분합니다."),
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7), wraplength=180, justify="left",
        ).pack(anchor="w", pady=(3, 8))
        tk.Label(
            self.build_presets_frame, text=self._tr("기본 추천 · OP.GG"),
            bg=COLORS["panel_2"], fg=COLORS["purple"],
            font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w", pady=(0, 5))
        for index, rune_build in enumerate(guide.rune_builds[:3]):
            keystone = (
                self._rune_name_text(
                    rune_build.perks[0].asset_id, rune_build.perks[0].name,
                )
                if rune_build.perks else rune_build.name
            )
            selected = index == self._build_rune_index
            preset_bg = "#4b3470" if selected else COLORS["chip"]
            select_preset = lambda selected_index=index: self._set_build_rune_index(
                selected_index
            )
            preset_card = tk.Frame(
                self.build_presets_frame, bg=preset_bg, padx=5, pady=5,
                highlightthickness=1,
                highlightbackground=COLORS["purple"] if selected else COLORS["border"],
            )
            preset_card.pack(fill="x", pady=(0, 6))
            icon_strip = tk.Frame(preset_card, bg=preset_bg)
            icon_strip.pack(side="right", padx=(4, 0))
            if rune_build.perks:
                keystone_asset = self.rune_catalog.asset(
                    rune_build.perks[0].asset_id, rune_build.perks[0]
                )
                self._build_inline_asset_icon(
                    icon_strip, keystone_asset, "rune", select_preset,
                    size=26, background=preset_bg,
                ).pack(side="left", padx=1)
            representative_item = representative_build_item(guide.item_groups, index)
            if representative_item:
                self._build_inline_asset_icon(
                    icon_strip, representative_item, "item", select_preset,
                    size=26, background=preset_bg,
                ).pack(side="left", padx=1)
            preset = tk.Button(
                preset_card,
                text=(
                    f"{self._text('build.default_name', index=index + 1)}\n{keystone}\n"
                    f"{self._build_stat_text(rune_build.games, rune_build.win_rate)}"
                ),
                command=select_preset,
                bg=preset_bg,
                fg=COLORS["text"] if selected else COLORS["muted"],
                activebackground="#5b4184" if selected else "#263a59",
                activeforeground=COLORS["text"],
                relief="flat", bd=0, cursor="hand2",
                anchor="w", justify="left", padx=4, pady=2,
                font=("Malgun Gothic", 8, "bold"),
            )
            preset.pack(side="left", fill="both", expand=True)
            self._build_preset_widgets.append(
                (preset_card, preset, icon_strip, index)
            )
        focus, matchup_reason = matchup_build_reason(
            self.draft.selected_enemy_support_id
        )
        enemy_name = (
            self._champion_text(self.draft.selected_enemy_support_id)
            if self.draft.selected_enemy_support_id else self._tr("상대 미확정")
        )
        matchup_runes = guide.rune_builds[matchup_index]
        matchup_keystone = (
            self._rune_name_text(
                matchup_runes.perks[0].asset_id, matchup_runes.perks[0].name,
            ) if matchup_runes.perks else matchup_runes.name
        )
        matchup_card = tk.Frame(
            self.build_presets_frame, bg="#172b34", padx=9, pady=8,
            highlightthickness=1, highlightbackground="#285e59",
        )
        matchup_card.pack(fill="x", pady=(8, 0))
        tk.Label(
            matchup_card,
            text=(
                f"{self._text('build.matchup_title', enemy=enemy_name)}\n"
                f"{self._tr(focus)}\n"
                f"{self._text('build.matchup_keystone', keystone=matchup_keystone, stats=self._build_stat_text(matchup_runes.games, matchup_runes.win_rate))}\n"
                f"{self._tr(matchup_reason)}"
            ),
            bg="#172b34", fg=COLORS["green"], justify="left", anchor="w",
            wraplength=180, font=("Malgun Gothic", 7, "bold"),
        ).pack(fill="x")
        matchup_assets = tk.Frame(matchup_card, bg="#172b34")
        matchup_assets.pack(fill="x", pady=(6, 0))
        tk.Label(
            matchup_assets, text=self._tr("대표 룬 · 아이템"),
            bg="#172b34", fg=COLORS["muted"],
            font=("Malgun Gothic", 6, "bold"),
        ).pack(side="left")
        matchup_icon_strip = tk.Frame(matchup_assets, bg="#172b34")
        matchup_icon_strip.pack(side="right")
        if matchup_runes.perks:
            matchup_keystone_asset = self.rune_catalog.asset(
                matchup_runes.perks[0].asset_id, matchup_runes.perks[0]
            )
            self._build_inline_asset_icon(
                matchup_icon_strip, matchup_keystone_asset, "rune",
                self._select_matchup_rune, size=26, background="#172b34",
            ).pack(side="left", padx=1)
        matchup_representative_item = representative_build_item(
            matchup_item_groups(
                guide.item_groups, self.draft.selected_enemy_support_id
            )
        )
        if matchup_representative_item:
            self._build_inline_asset_icon(
                matchup_icon_strip, matchup_representative_item, "item",
                self._select_matchup_rune, size=26, background="#172b34",
            ).pack(side="left", padx=1)
        matchup_button = self._button(
            matchup_card, "상대 대응 룬 보기", self._select_matchup_rune,
            COLORS["green"], width=18,
        )
        matchup_button.configure(pady=4, font=("Malgun Gothic", 7, "bold"))
        matchup_button.pack(fill="x", pady=(7, 0))
        selected_runes = guide.rune_builds[
            min(self._build_rune_index, len(guide.rune_builds) - 1)
        ]
        self._render_rune_editor(selected_runes)

        tk.Label(
            self.build_spells_frame, text=self._tr("소환사 주문"), bg=COLORS["panel_2"],
            fg=COLORS["blue"], font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")
        self._render_spell_build_choices(self.build_spells_frame, guide)
        tk.Label(
            self.build_spells_frame, text=self._tr("선택 조합 · D / F 배치"),
            bg=COLORS["panel_2"], fg=COLORS["blue"],
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w", pady=(4, 0))
        spell_row = tk.Frame(self.build_spells_frame, bg=COLORS["panel_2"])
        spell_row.pack(fill="x", pady=(5, 14))
        self._build_spell_row = spell_row
        self._render_spell_assets_row(spell_row, guide)
        flash_row = tk.Frame(self.build_spells_frame, bg=COLORS["panel_2"])
        flash_row.pack(fill="x", pady=(0, 12))
        tk.Label(
            flash_row, text=self._tr("점멸 위치"), bg=COLORS["panel_2"],
            fg=COLORS["muted"], font=("Malgun Gothic", 8, "bold"),
        ).pack(side="left", padx=(0, 6))
        for slot in ("D", "F"):
            button = self._button(
                flash_row, self._text("build.flash_slot", slot=slot),
                lambda selected=slot: self._set_flash_slot(selected),
                COLORS["blue"] if slot == self._flash_slot else COLORS["muted"],
                width=8,
            )
            button.configure(
                bg="#214a6b" if slot == self._flash_slot else COLORS["chip"],
                pady=4, font=("Malgun Gothic", 7, "bold"),
            )
            setattr(
                button, "_advisor_base_bg",
                "#214a6b" if slot == self._flash_slot else COLORS["chip"],
            )
            button.pack(side="left", padx=(0, 4))
            self._flash_slot_buttons[slot] = button
        tk.Label(
            self.build_spells_frame, text=self._tr("스킬 강화 순서"), bg=COLORS["panel_2"],
            fg=COLORS["gold"], font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w")
        priority = "  →  ".join(guide.skill_priority) or self._tr("데이터 없음")
        tk.Label(
            self.build_spells_frame, text=priority, bg=COLORS["panel_2"],
            fg=COLORS["text"], font=("Consolas", 18, "bold"),
        ).pack(anchor="w", pady=(6, 10))
        if guide.skill_sequence:
            self._render_skill_grid(self.build_spells_frame, guide)
        spell_apply = self._button(
            self.build_spells_frame, "추천 스펠 적용",
            lambda: self._apply_build_component("spells"), COLORS["blue"], width=18,
        )
        spell_apply.pack(fill="x", pady=(12, 0))
        self.build_apply_buttons.append(spell_apply)

        tk.Label(
            self.build_items_frame, text=self._tr("아이템 빌드"), bg=COLORS["panel_2"],
            fg=COLORS["green"], font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")
        base_builds = final_item_builds(
            guide.item_groups, guide.position, limit=3
        )
        matchup_builds = matchup_final_item_builds(
            guide.item_groups, self.draft.selected_enemy_support_id,
            guide.position, limit=2,
        )

        def render_complete_section(
            title: str, builds: list[BuildItemGroup],
            accent: str, background: str, border: str,
        ) -> None:
            tk.Label(
                self.build_items_frame, text=self._tr(title),
                bg=COLORS["panel_2"], fg=accent,
                font=("Malgun Gothic", 8, "bold"),
            ).pack(anchor="w", pady=(8, 4))
            for index, item_build in enumerate(builds, start=1):
                final_card = tk.Frame(
                    self.build_items_frame, bg=background, padx=6, pady=6,
                    highlightthickness=1, highlightbackground=border,
                )
                final_card.pack(fill="x", pady=(0, 6))
                tk.Label(
                    final_card, text=f"{self._tr(title.split('·')[0].strip())} {index}",
                    bg=background, fg=accent,
                    font=("Malgun Gothic", 7, "bold"),
                ).pack(anchor="w", pady=(0, 4))
                final_grid = tk.Frame(final_card, bg=background)
                final_grid.pack(fill="x")
                for column, item in enumerate(item_build.items):
                    self._build_asset_widget(
                        final_grid, item, "item", 28, compact=True
                    ).grid(row=0, column=column, sticky="nsew", padx=1)
                    final_grid.grid_columnconfigure(
                        column, weight=1, uniform="final_items"
                    )

        render_complete_section(
            "기본 추천 완성 빌드 · OP.GG 순서", base_builds,
            COLORS["gold"], COLORS["surface"], COLORS["border"],
        )
        if not base_builds:
            tk.Label(
                self.build_items_frame,
                text=self._tr("완성 아이템 후보가 6개 미만입니다."),
                bg=COLORS["panel_2"], fg=COLORS["orange"],
                font=("Malgun Gothic", 7),
            ).pack(anchor="w")
        if self.draft.selected_enemy_support_id:
            render_complete_section(
                self._text("build.enemy_completed", enemy=enemy_name),
                matchup_builds, COLORS["green"], "#172b34", "#285e59",
            )

        candidate_count = sum(min(len(group.items), 10) for group in guide.item_groups)
        detail_toggle = self._button(
            self.build_items_frame,
            (
                self._text("build.detail_collapse", count=candidate_count)
                if self._build_item_details_expanded
                else self._text("build.detail_expand", count=candidate_count)
            ),
            self._toggle_build_item_details, COLORS["green"],
        )
        detail_toggle.pack(fill="x", pady=(10, 2))
        self._build_item_details_toggle = detail_toggle
        detail_frame = tk.Frame(self.build_items_frame, bg=COLORS["panel_2"])
        self._build_item_details_frame = detail_frame
        if self._build_item_details_expanded:
            self._populate_build_item_details(detail_frame, guide)
            detail_frame.pack(fill="x")
        item_apply = self._button(
            self.build_items_frame, "정리된 아이템 세트 적용",
            lambda: self._apply_build_component("items"), COLORS["gold"], width=20,
        )
        item_apply.pack(fill="x", pady=(12, 0))
        self._build_item_apply_button = item_apply
        self.build_apply_buttons.append(item_apply)
        for button in self.build_apply_buttons:
            button.configure(
                state="disabled" if self._build_applying or self.demo else "normal"
            )
        if self.demo:
            self.build_apply_status.configure(
                text=self._tr("데모 화면에서는 롤 클라이언트를 변경하지 않습니다."), fg=COLORS["gold"]
            )
        self._mark_build_render_current()

    def _populate_build_item_details(
        self, detail_frame: tk.Frame, guide: ChampionBuildGuide,
    ) -> None:
        if detail_frame.winfo_children():
            return
        tk.Label(
            detail_frame, text=self._tr("핵심 · 순서별 · 상황별 아이템 후보"),
            bg=COLORS["panel_2"], fg=COLORS["green"],
            font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w", pady=(8, 2))
        for group in guide.item_groups:
            tk.Label(
                detail_frame, text=self._tr(group.title), bg=COLORS["panel_2"],
                fg=COLORS["muted"], font=("Malgun Gothic", 8, "bold"),
            ).pack(anchor="w", pady=(8, 3))
            item_grid = tk.Frame(detail_frame, bg=COLORS["panel_2"])
            item_grid.pack(fill="x")
            for index, item in enumerate(group.items[:10]):
                self._build_asset_widget(item_grid, item, "item", 30).grid(
                    row=index // 5, column=index % 5,
                    sticky="nsew", padx=2, pady=2,
                )
            for column in range(5):
                item_grid.grid_columnconfigure(column, weight=1, uniform="item_options")

    def _toggle_build_item_details(self) -> None:
        old_bounds = self.build_canvas.bbox("all")
        old_height = max(
            float(old_bounds[3] - old_bounds[1]) if old_bounds else 1.0, 1.0
        )
        canvas_y = max(float(self.build_canvas.yview()[0]) * old_height, 0.0)
        self._build_item_details_expanded = not self._build_item_details_expanded
        detail_frame = self._build_item_details_frame
        toggle = self._build_item_details_toggle
        apply_button = self._build_item_apply_button
        if detail_frame and detail_frame.winfo_exists():
            if self._build_item_details_expanded:
                if self.build_guide:
                    self._populate_build_item_details(detail_frame, self.build_guide)
                if apply_button and apply_button.winfo_exists():
                    detail_frame.pack(fill="x", before=apply_button)
                else:
                    detail_frame.pack(fill="x")
            else:
                detail_frame.pack_forget()
        if toggle and toggle.winfo_exists() and self.build_guide:
            candidate_count = sum(
                min(len(group.items), 10) for group in self.build_guide.item_groups
            )
            toggle.configure(text=(
                f"상세 아이템 후보 접기 · {candidate_count}개"
                if self._build_item_details_expanded
                else f"상세 아이템 후보 펼치기 · {candidate_count}개"
            ))
        self._mark_build_render_current()

        def restore_scroll_position() -> None:
            self.root.update_idletasks()
            bounds = self.build_canvas.bbox("all")
            if not bounds:
                return
            content_height = max(float(bounds[3] - bounds[1]), 1.0)
            self.build_canvas.configure(scrollregion=bounds)
            self.build_canvas.yview_moveto(min(canvas_y / content_height, 1.0))

        self.root.after(80, restore_scroll_position)

    def _apply_build_component(self, component: str) -> None:
        if self.demo or self._build_applying or not self.build_guide:
            return
        guide = self.build_guide
        if guide.champion_id != self._build_selected_champion_id \
                or guide.position != self.draft.my_role or not guide.rune_builds:
            messagebox.showwarning(
                "빌드 불일치", "현재 챔피언과 포지션의 빌드를 먼저 불러오세요.",
                parent=self.root,
            )
            return
        base_rune_build = guide.rune_builds[
            min(self._build_rune_index, len(guide.rune_builds) - 1)
        ]
        rune_build = self._active_rune_build(base_rune_build)
        selected_spell_build = self._selected_spell_build(guide)
        base_builds = [
            BuildItemGroup(f"기본 추천 완성 빌드 {index}", build.items)
            for index, build in enumerate(
                final_item_builds(guide.item_groups, guide.position, limit=3), start=1
            )
        ]
        matchup_builds = [
            BuildItemGroup(
                f"{self.registry.ko_name(self.draft.selected_enemy_support_id)} "
                f"대응 완성 빌드 {index}",
                build.items,
            )
            for index, build in enumerate(
                matchup_final_item_builds(
                    guide.item_groups, self.draft.selected_enemy_support_id,
                    guide.position, limit=2,
                ),
                start=1,
            )
        ]
        apply_guide = replace(
            guide,
            summoner_spells=(
                list(selected_spell_build.spells)
                if selected_spell_build else list(guide.summoner_spells)
            ),
            item_groups=[*base_builds, *matchup_builds, *guide.item_groups],
        )
        self._build_applying = True
        self.build_apply_status.configure(
            text="롤 클라이언트에 적용 중...", fg=COLORS["blue"]
        )
        for button in self.build_apply_buttons:
            button.configure(state="disabled")
        self._mark_build_render_current()

        def work() -> str:
            if component == "runes":
                return self.build_applicator.apply_runes(
                    apply_guide, rune_build, self.ui_language,
                )
            if component == "spells":
                return self.build_applicator.apply_spells(
                    apply_guide, self._flash_slot
                )
            if component == "items":
                return self.build_applicator.apply_item_set(apply_guide)
            return "\n".join(
                self.build_applicator.apply_all(
                    apply_guide, rune_build, self._flash_slot, self.ui_language
                )
            )

        def success(message: str) -> None:
            self._build_applying = False
            self.build_apply_status.configure(text=message, fg=COLORS["green"])
            for button in self.build_apply_buttons:
                button.configure(state="normal")
            self._mark_build_render_current()

        def error(exc: Exception) -> None:
            self._build_applying = False
            self.build_apply_status.configure(text=str(exc), fg=COLORS["red"])
            for button in self.build_apply_buttons:
                button.configure(state="normal")
            self._mark_build_render_current()
            messagebox.showerror("빌드 적용 실패", str(exc), parent=self.root)

        self._background(work, success, error)

    def _personal_stats_for(
        self, champion_ids: list[str]
    ) -> dict[str, PersonalStat | None]:
        puuid = self.storage.get_setting("riot_puuid")
        if not puuid:
            return {champion_id: None for champion_id in champion_ids}
        adc = allied_adc_member(self.draft) if self.draft.my_role == "SUPPORT" else None
        context = (
            self.storage.match_revision(),
            puuid,
            self.draft.my_role,
            self.draft.selected_enemy_support_id,
            adc.champion_id if adc else None,
        )
        if context != self._personal_cache_context:
            self._personal_cache_context = context
            self._personal_stats_cache.clear()
            self._personal_stats_pending.clear()
        for champion_id in champion_ids:
            if champion_id and champion_id not in self._personal_stats_cache:
                self._personal_stats_pending.add(champion_id)
        self._schedule_personal_stats_load()
        return {
            champion_id: self._personal_stats_cache.get(champion_id)
            for champion_id in champion_ids
        }

    def _schedule_personal_stats_load(self) -> None:
        if self._personal_load_scheduled or self._personal_stats_loading:
            return
        if not self._personal_stats_pending:
            return
        self._personal_load_scheduled = True
        self.root.after(25, self._start_personal_stats_load)

    def _start_personal_stats_load(self) -> None:
        self._personal_load_scheduled = False
        if self._personal_stats_loading or not self._personal_stats_pending:
            return
        context = self._personal_cache_context
        if context is None:
            return
        champion_ids = sorted(self._personal_stats_pending)
        self._personal_stats_pending.clear()
        self._personal_stats_loading = True
        _revision, puuid, position, enemy_support_id, ally_adc_id = context

        def success(stats: dict[str, PersonalStat]) -> None:
            self._personal_stats_loading = False
            if context == self._personal_cache_context:
                self._personal_stats_cache.update(stats)
                self._schedule_selection_render()
            self._schedule_personal_stats_load()

        def error(_exc: Exception) -> None:
            self._personal_stats_loading = False
            self._schedule_personal_stats_load()

        self._background(
            lambda: self.storage.personal_stats(
                puuid, champion_ids, enemy_support_id, ally_adc_id,
                limit=1000, position=position,
            ),
            success,
            error,
        )

    def _render_header(self) -> None:
        self._render_automation_toggles()
        if self.demo:
            text, color = (
                self._text("header.connection.demo"),
                COLORS["gold"],
            )
        elif self.game_phase == "InProgress":
            text, color = self._text("header.connection.game"), COLORS["green"]
        elif self.draft.connection_state == "CHAMP_SELECT":
            text, color = self._text("header.connection.draft"), COLORS["green"]
        elif self.draft.connection_state == "LOBBY":
            text, color = self._text("header.connection.lobby"), COLORS["blue"]
        else:
            text, color = self._text("header.connection.waiting"), COLORS["muted"]
        self.connection_label.configure(text=text, fg=color)
        if self.demo:
            self.opgg_header_label.configure(text=self._text("header.opgg.demo"))
        elif self.opgg_meta_snapshot or self.opgg_snapshot:
            header_snapshot = self.opgg_meta_snapshot or self.opgg_snapshot
            assert header_snapshot is not None
            stamp = header_snapshot.updated_at.replace("T", " ")[:16]
            self.opgg_header_label.configure(
                text=self._text(
                    "header.opgg.cached", patch=header_snapshot.patch, stamp=stamp,
                )
            )
        else:
            self.opgg_header_label.configure(text="OP.GG 캐시 없음")
        if self._opgg_refreshing:
            self.opgg_button.configure(state="disabled", text="메타 갱신 중...")
        else:
            self.opgg_button.configure(state="normal", text="통계 캐시 확인")
        if self._riot_syncing:
            self.riot_button.configure(state="disabled")
        elif self._riot_history_cooldown_remaining().total_seconds() > 0:
            remaining = self._riot_history_cooldown_remaining()
            hours = int(remaining.total_seconds()) // 3600
            minutes = (int(remaining.total_seconds()) % 3600) // 60
            self.riot_button.configure(
                state="disabled", text=self._text(
                    "header.riot.cooldown", hours=hours, minutes=minutes,
                )
            )
        else:
            self.riot_button.configure(state="normal", text="전적 갱신")
        key = self.storage.get_setting("riot_api_key")
        key_remaining = self.storage.riot_api_key_refresh_remaining()
        if self.demo:
            self.api_key_status_label.configure(
                text="DEMO · 실제 Riot API 키 미사용", fg=COLORS["gold"],
            )
            self.settings_button.configure(text="가상 데이터 전용", state="disabled")
            self.riot_button.configure(text="더미 전적", state="disabled")
        elif not key:
            self.api_key_status_label.configure(text="Riot API 키 없음", fg=COLORS["red"])
            self.settings_button.configure(text="Riot 설정")
        elif key_remaining.total_seconds() <= 0:
            self.api_key_status_label.configure(
                text="개발용 API 키 갱신 필요 · 24시간 만료", fg=COLORS["red"]
            )
            self.settings_button.configure(text="API 키 갱신")
        else:
            seconds = int(key_remaining.total_seconds())
            hours, seconds = divmod(seconds, 3600)
            minutes = seconds // 60
            self.api_key_status_label.configure(
                text=self._text(
                    "header.key.remaining", hours=hours, minutes=minutes,
                ),
                fg=COLORS["green"],
            )
            self.settings_button.configure(text="Riot 설정")

        phase_value = "DEMO" if self.demo else {
            "InProgress": "PLAY",
        }.get(self.game_phase, "DRAFT" if self.draft.connection_state == "CHAMP_SELECT" else "대기 중")
        phase_detail = (
            "화면 예시 · 읽기 전용" if self.demo else
            "플레이 탭 자동 전환" if self.game_phase == "InProgress" else
            "실시간 픽·밴 감지" if self.draft.connection_state == "CHAMP_SELECT" else
            "클라이언트 연결 대기"
        )
        self.header_metrics["phase"][0].configure(text=phase_value, fg=color)
        self.header_metrics["phase"][1].configure(text=phase_detail)
        role_name = self._position_text(self.draft.my_role)
        target = (
            self._champion_text(
                self.draft.selected_enemy_support_id,
                self.draft.selected_enemy_support_name_ko,
            )
            if self.draft.selected_enemy_support_id else self._tr("블라인드")
        )
        self.app_title_label.configure(text=f"LOL {role_name.upper()} PICK ADVISOR")
        self.root.title(f"LOL Pick Advisor · {role_name}")
        order = (
            self._text("header.draft.order", order=self.draft.my_pick_order)
            if self.draft.my_pick_order else self._tr("픽 순서 미확인")
        )
        self.header_metrics["draft"][0].configure(text=target)
        self.header_metrics["draft"][1].configure(
            text=self._text("header.draft.detail", role=role_name, order=order)
        )
        match_count = self.storage.count_matches()
        self.header_metrics["cache"][0].configure(
            text=self._tr("가상 표본") if self.demo else self._text(
                "common.matches", count=match_count,
            )
        )
        self.header_metrics["cache"][1].configure(
            text=(
                "코드에 포함된 공개용 더미 데이터"
                if self.demo else "내 전적·관계 기록 로컬 계산"
            )
        )
        key_ready = bool(key and key_remaining.total_seconds() > 0)
        opgg_ready = bool(self.opgg_meta_snapshot or self.opgg_snapshot)
        data_value = (
            "DEMO DATA" if self.demo else
            "READY" if key_ready and opgg_ready else
            "부분 준비" if key_ready or opgg_ready else "설정 필요"
        )
        data_color = (
            COLORS["gold"] if self.demo else
            COLORS["green"] if data_value == "READY" else
            COLORS["orange"] if data_value == "부분 준비" else COLORS["red"]
        )
        self.header_metrics["data"][0].configure(text=data_value, fg=data_color)
        self.header_metrics["data"][1].configure(
            text=(
                self._tr("외부 계정 연결 없음")
                if self.demo else
                self._text(
                    "header.data.detail",
                    riot=("OK" if key_ready else (
                        "refresh required" if self.ui_language == "en" else "갱신 필요"
                    )),
                    opgg=("OK" if opgg_ready else (
                        "none" if self.ui_language == "en" else "없음"
                    )),
                )
            )
        )
        if self.ui_language == "en":
            self._translate_widget_tree(self.header_frame)

    def _draft_team_slots(self, ally: bool) -> list[DraftMember | None]:
        members = list(
            self.draft.ally_team_order if ally else self.draft.enemy_team_order
        )
        if not members:
            if ally:
                members = [*self.draft.ally_locked, *self.draft.ally_hover]
                if self.draft.my_hover:
                    members.append(self.draft.my_hover)
            else:
                members = list(self.draft.enemy_locked)
            members.sort(key=lambda member: (
                member.pick_order is None,
                member.pick_order if member.pick_order is not None else 99,
                member.cell_id if member.cell_id is not None else 99,
            ))
        return [*members[:5], *([None] * max(0, 5 - len(members)))]

    def _render_draft_team_slots(self, frame: tk.Frame, ally: bool) -> None:
        widgets = getattr(frame, "_advisor_draft_slot_widgets", None)
        if not widgets:
            widgets = []
            self._clear(frame)
            for column in range(5):
                frame.grid_columnconfigure(column, weight=1, uniform="draft_team_slots")
                outer = tk.Frame(frame, bg=COLORS["border"], padx=1, pady=1)
                outer.grid(
                    row=0, column=column, sticky="nsew",
                    padx=(0 if column == 0 else 3, 3),
                )
                button = tk.Button(
                    outer, image="", compound="left", justify="left", anchor="w",
                    relief="flat", bd=0, padx=9, pady=7,
                    bg=COLORS["panel_2"], fg=COLORS["muted"],
                    disabledforeground=COLORS["muted"],
                    activebackground=COLORS["surface_selected"],
                    activeforeground=COLORS["text"],
                    font=("Malgun Gothic", 8, "bold"),
                )
                button.pack(fill="both", expand=True)
                widgets.append((outer, button))
            setattr(frame, "_advisor_draft_slot_widgets", widgets)
        selected_id = self.draft.selected_enemy_support_id
        for index, member in enumerate(self._draft_team_slots(ally)):
            outer, widget = widgets[index]
            is_me = bool(
                ally and member
                and self.draft.local_player_cell_id is not None
                and member.cell_id == self.draft.local_player_cell_id
            )
            selected = bool(
                not ally and member and member.champion_id
                and member.champion_id == selected_id
            )
            state = member.state if member else "EMPTY"
            accent = (
                COLORS["gold"] if is_me else
                COLORS["blue"] if selected or ally else COLORS["red"]
            )
            border = accent if (is_me or selected or state != "EMPTY") else COLORS["border"]
            role = ROLE_LABELS.get(member.role, "?") if member else "?"
            name = (
                self._champion_text(member.champion_id, member.champion_name_ko)
                if member and member.champion_id
                else self._text("draft.slot.waiting")
            )
            status = {
                "LOCKED": self._text("draft.slot.locked"),
                "HOVER": self._text("draft.slot.hover"),
                "EMPTY": self._text("draft.slot.empty"),
            }.get(state, state)
            order = member.pick_order if member and member.pick_order else index + 1
            turn_text = self._text(
                "draft.slot.turn", turn=member.pick_turn,
            ) if member and member.pick_turn is not None else ""
            prefix = self._text("draft.slot.me") if is_me else ""
            icon = (
                self.icon_cache.get(
                    member.champion_id, 42, self._selection_icon_ready("draft")
                )
                if member and member.champion_id else None
            )
            selectable = bool(
                not ally and member and member.champion_id
                and state in {"LOCKED", "HOVER"}
            )
            if selectable and member:
                command = lambda champion_id=member.champion_id: self._select_enemy_support(
                    champion_id
                )
            else:
                command = lambda: None
            background = COLORS["surface_selected"] if selected else COLORS["panel_2"]
            outer.configure(bg=border)
            widget.configure(
                text=self._text(
                    "draft.slot", order=order, turn=turn_text,
                    me=prefix, role=role, champion=name, status=status,
                ),
                image=icon or "", bg=background,
                fg=accent if state != "EMPTY" else COLORS["muted"],
                disabledforeground=accent if state != "EMPTY" else COLORS["muted"],
                command=command,
                cursor="hand2" if selectable else "arrow",
                state="normal" if selectable else "disabled",
            )
            setattr(widget, "_advisor_image", icon)

    def _render_draft_bans(self, frame: tk.Frame, ally: bool) -> None:
        widgets = getattr(frame, "_advisor_draft_ban_widgets", None)
        if not widgets:
            widgets = []
            self._clear(frame)
            for _index in range(5):
                label = tk.Label(
                    frame, image="", compound="left", bg=COLORS["chip"],
                    fg=COLORS["muted"], padx=7, pady=5,
                    font=("Malgun Gothic", 7, "bold"),
                )
                label.pack(side="left", padx=(0, 4), pady=1)
                widgets.append(label)
            setattr(frame, "_advisor_draft_ban_widgets", widgets)
        actions = list(
            self.draft.ally_ban_actions if ally else self.draft.enemy_ban_actions
        )
        completed_ids = self.draft.ally_bans if ally else self.draft.enemy_bans
        for index in range(5):
            if index < len(actions):
                action = actions[index]
                champion_id = action.champion_id
                state = action.state
            elif index < len(completed_ids):
                champion_id = completed_ids[index]
                state = "LOCKED"
            else:
                champion_id = ""
                state = "EMPTY"
            name = self._champion_text(champion_id) if champion_id else "--"
            status = (
                self._text("draft.ban.locked") if state == "LOCKED" else
                self._text("draft.ban.hover") if state == "HOVER" else
                self._text("draft.ban.waiting")
            )
            color = (
                COLORS["red"] if state == "LOCKED" else
                COLORS["orange"] if state == "HOVER" else COLORS["muted"]
            )
            icon = (
                self.icon_cache.get(
                    champion_id, 26, self._selection_icon_ready("draft")
                )
                if champion_id else None
            )
            label = widgets[index]
            label.configure(
                text=self._text(
                    "draft.ban", order=index + 1, champion=name, status=status,
                ),
                image=icon or "", fg=color,
            )
            setattr(label, "_advisor_image", icon)

    def _render_draft(self) -> None:
        role_name = self._position_text(self.draft.my_role)
        order = (
            self._text("draft.order.team", order=self.draft.my_pick_order)
            if self.draft.my_pick_order else self._text("draft.order.unknown")
        )
        states = {
            "WAITING": self._text("draft.state.waiting"),
            "SELECTING": self._text("draft.state.selecting"),
            "LOCKED": self._text("draft.state.locked"),
        }
        unknown_selected = self.draft.selected_enemy_support_source == "MANUAL_UNKNOWN"
        stale = self._recommendations_stale()
        revision = self._selection_panel_revisions.get("draft", 0)
        swap_note = {
            "SENT": self._text("draft.swap.sent"),
            "RECEIVED": self._text("draft.swap.received"),
            "ACCEPTED": self._text("draft.swap.accepted"),
        }.get(self.draft.pick_order_swap_state, "")
        notice = self._pick_order_change_notice or swap_note

        if self._selection_panel_needs_render(
            "draft_header", revision, self.draft.my_role,
            self.draft.my_pick_order, self.draft.my_status,
            self.draft.snapshot_id, notice,
            self.draft.selected_enemy_support_id,
            self.draft.selected_enemy_support_name_ko,
            self.draft.selected_enemy_support_source, stale,
        ):
            notice_text = f"    {notice}" if notice else ""
            self.pick_order_label.configure(
                text=self._text(
                    "draft.summary", role=role_name, order=order,
                    state=states.get(self.draft.my_status, self.draft.my_status),
                    notice=notice_text, snapshot=self.draft.snapshot_id,
                )
            )
            self.enemy_instruction_label.configure(
                text=self._text("draft.enemy.instruction", role=role_name)
            )
            self.enemy_unknown_button.configure(
                text=self._text("draft.enemy.unknown_button", role=role_name)
            )
            self._set_button_selected(
                self.enemy_unknown_button, unknown_selected, COLORS["orange"]
            )
            if unknown_selected:
                self.enemy_support_label.configure(
                    text=self._text("draft.enemy.selected_unknown", role=role_name)
                )
            elif self.draft.selected_enemy_support_id:
                source = (
                    self._text("draft.enemy.manual")
                    if self.draft.selected_enemy_support_source == "MANUAL_ENEMY_SUPPORT"
                    else self._text("draft.enemy.auto")
                )
                self.enemy_support_label.configure(
                    text=self._text(
                        "draft.enemy.selected", role=role_name,
                        champion=self._champion_text(
                            self.draft.selected_enemy_support_id,
                            self.draft.selected_enemy_support_name_ko,
                        ),
                        source=source,
                    )
                )
            else:
                self.enemy_support_label.configure(
                    text=self._text("draft.enemy.pending", role=role_name)
                )
            self.stale_label.configure(
                text=self._text("draft.stale") if stale else ""
            )

        ally_ban_signature = tuple(
            (item.champion_id, item.state, item.actor_cell_id, item.order)
            for item in self.draft.ally_ban_actions
        )
        if self._selection_panel_needs_render(
            "draft_ally_bans", revision, ally_ban_signature,
            tuple(self.draft.ally_bans),
        ):
            self._render_draft_bans(self.ally_bans_frame, ally=True)

        enemy_ban_signature = tuple(
            (item.champion_id, item.state, item.actor_cell_id, item.order)
            for item in self.draft.enemy_ban_actions
        )
        if self._selection_panel_needs_render(
            "draft_enemy_bans", revision, enemy_ban_signature,
            tuple(self.draft.enemy_bans),
        ):
            self._render_draft_bans(self.enemy_bans_frame, ally=False)

        def member_signature(member: DraftMember | None) -> tuple[object, ...]:
            if member is None:
                return (None,)
            return (
                member.champion_id, member.role, member.state, member.cell_id,
                member.pick_order, member.pick_turn,
            )

        if self._selection_panel_needs_render(
            "draft_ally_picks", revision,
            tuple(member_signature(member) for member in self._draft_team_slots(True)),
            self.draft.local_player_cell_id,
        ):
            self._render_draft_team_slots(self.ally_picks_frame, ally=True)
        if self._selection_panel_needs_render(
            "draft_enemy_picks", revision,
            tuple(member_signature(member) for member in self._draft_team_slots(False)),
            self.draft.selected_enemy_support_id,
        ):
            self._render_draft_team_slots(self.enemy_picks_frame, ally=False)

    def _schedule_hover_matchup_render(self) -> None:
        if self._hover_matchup_render_scheduled:
            return
        self._hover_matchup_render_scheduled = True

        def render() -> None:
            self._hover_matchup_render_scheduled = False
            self._render_hover_matchup_card()

        self.root.after(50, render)

    def _set_hover_matchup_icon(
        self, widget: tk.Label, champion_id: str | None,
    ) -> None:
        image = (
            self.icon_cache.get(
                champion_id, 40, self._schedule_hover_matchup_render
            )
            if champion_id else None
        )
        widget.configure(image=image or "", text="" if image else "?")

    def _render_hover_matchup_card(self) -> None:
        if not hasattr(self, "hover_matchup_card"):
            return
        hover = local_draft_selection(self.draft)
        enemy_id = self.draft.selected_enemy_support_id
        position = self.draft.my_role
        snapshot = self.opgg_snapshot
        if snapshot and snapshot.enemy_support_id != enemy_id:
            snapshot = None
        cache_key = f"{position}:{enemy_id}".upper() if enemy_id else ""
        fetching = bool(cache_key and cache_key in self._selection_matchup_refreshing)
        personal = self._hover_personal_stat
        synergy = self._synergy_for(hover.champion_id) if hover else None
        signature = repr((
            hover, enemy_id, self.draft.selected_enemy_support_source, position,
            snapshot, fetching, self._hover_matchup_errors.get(cache_key),
            self._hover_personal_context, personal, synergy,
            self.icon_cache.is_cached(hover.champion_id) if hover else False,
            self.icon_cache.is_cached(enemy_id) if enemy_id else False,
        ))
        if signature == self._hover_matchup_signature:
            return
        self._hover_matchup_signature = signature

        if not hover or not hover.champion_id:
            self.hover_matchup_badge.configure(
                text=self._text("hover.waiting.badge"),
                bg=COLORS["chip"], fg=COLORS["muted"]
            )
            self.hover_matchup_title.configure(text=self._text("hover.waiting.title"))
            self.hover_matchup_cache.configure(text=self._tr("로컬 캐시 우선"), fg=COLORS["muted"])
            self._set_hover_matchup_icon(self.hover_matchup_ally_icon, None)
            self._set_hover_matchup_icon(self.hover_matchup_enemy_icon, enemy_id)
            self.hover_matchup_ally_name.configure(
                text=self._text("hover.waiting.ally"), fg=COLORS["muted"]
            )
            self.hover_matchup_enemy_name.configure(
                text=(
                    self._champion_text(enemy_id)
                    if enemy_id else self._text("hover.waiting.enemy")
                ),
                fg=COLORS["red"] if enemy_id else COLORS["muted"],
            )
            self.hover_matchup_rate.configure(
                text=self._text("hover.waiting.rate"), fg=COLORS["muted"]
            )
            self.hover_matchup_detail.configure(
                text=self._text("hover.waiting.detail")
            )
            self.hover_matchup_local.configure(
                text=self._text("hover.waiting.local"), fg=COLORS["muted"]
            )
            self.hover_matchup_card.configure(highlightbackground=COLORS["divider"])
            return

        hover_name = self._champion_text(hover.champion_id, hover.champion_name_ko)
        locked = hover.state == "LOCKED"
        self.hover_matchup_badge.configure(
            text=self._text("hover.locked.badge" if locked else "hover.intent.badge"),
            bg="#17362e" if locked else "#174667",
            fg=COLORS["green"] if locked else COLORS["blue"],
        )
        self.hover_matchup_title.configure(
            text=self._text(
                "hover.locked.title" if locked else "hover.intent.title",
                champion=hover_name,
            )
        )
        self._set_hover_matchup_icon(self.hover_matchup_ally_icon, hover.champion_id)
        self._set_hover_matchup_icon(self.hover_matchup_enemy_icon, enemy_id)
        self.hover_matchup_ally_name.configure(text=hover_name, fg=COLORS["blue"])
        self.hover_matchup_enemy_name.configure(
            text=(
                self._champion_text(enemy_id)
                if enemy_id else self._text("hover.enemy.pending")
            ),
            fg=COLORS["red"] if enemy_id else COLORS["orange"],
        )

        if not enemy_id:
            self.hover_matchup_cache.configure(
                text=self._text("hover.enemy.waiting"), fg=COLORS["orange"]
            )
            self.hover_matchup_rate.configure(
                text=self._text("hover.blind.rate", champion=hover_name),
                fg=COLORS["gold"]
            )
            self.hover_matchup_detail.configure(
                text=self._text(
                    "hover.blind.detail", role=self._position_text(position),
                )
            )
            self.hover_matchup_local.configure(
                text=self._text("hover.blind.local"),
                fg=COLORS["muted"],
            )
            self.hover_matchup_card.configure(highlightbackground=COLORS["orange"])
            return

        counter = matchup_counter_for_candidate(snapshot, hover.champion_id)
        fresh = bool(snapshot and self._matchup_snapshot_fresh(snapshot))
        error = self._hover_matchup_errors.get(cache_key, "")
        if fresh:
            cache_text, cache_color = self._text("hover.cache.fresh"), COLORS["green"]
        elif snapshot and fetching:
            cache_text, cache_color = self._text("hover.cache.refreshing"), COLORS["orange"]
        elif snapshot:
            cache_text, cache_color = self._text("hover.cache.stale"), COLORS["orange"]
        elif fetching:
            cache_text, cache_color = self._text("hover.cache.fetching"), COLORS["blue"]
        elif error:
            cache_text, cache_color = self._text("hover.cache.failed"), COLORS["orange"]
        else:
            cache_text, cache_color = self._text("hover.cache.missing"), COLORS["muted"]
        self.hover_matchup_cache.configure(text=cache_text, fg=cache_color)

        if counter:
            rate = counter.versus_win_rate
            result = self._matchup_label_text(rate)
            rate_color = (
                COLORS["green"] if rate >= 51.5 else
                COLORS["red"] if rate < 48.5 else COLORS["gold"]
            )
            self.hover_matchup_rate.configure(
                text=self._text(
                    "hover.matchup.rate", champion=hover_name,
                    rate=_fmt_rate(rate), result=result,
                ),
                fg=rate_color,
            )
            overall = (
                self._text(
                    "hover.matchup.overall",
                    rate=_fmt_rate(counter.overall_win_rate),
                )
                if counter.overall_win_rate is not None else ""
            )
            self.hover_matchup_detail.configure(
                text=self._text(
                    "hover.matchup.detail",
                    enemy=self._champion_text(enemy_id),
                    games=self._games_text(counter.games), overall=overall,
                    note=self._text(
                        "hover.matchup.note.refreshing"
                        if fetching else "hover.matchup.note.reference"
                    ),
                )
            )
            self.hover_matchup_card.configure(highlightbackground=rate_color)
        else:
            message = self._text(
                "hover.matchup.loading" if fetching else "hover.matchup.no_sample"
            )
            self.hover_matchup_rate.configure(
                text=self._text(
                    "hover.matchup.empty", champion=hover_name,
                    enemy=self._champion_text(enemy_id), message=message,
                ),
                fg=COLORS["blue"] if fetching else COLORS["orange"],
            )
            self.hover_matchup_detail.configure(
                text=self._text(
                    "hover.matchup.no_value",
                    error=(f" · {error}" if error and not fetching else ""),
                )
            )
            self.hover_matchup_card.configure(highlightbackground=COLORS["orange"])

        if self._hover_personal_context is None:
            local_parts = [self._text("hover.local.waiting")]
        elif self._hover_personal_context in self._hover_personal_loading:
            local_parts = [self._text("hover.local.loading")]
        elif personal and personal.matchup_games:
            local_parts = [
                self._text(
                    "hover.local.matchup", games=personal.matchup_games,
                    wins=personal.matchup_wins, losses=personal.matchup_losses,
                    rate=_fmt_rate(personal.matchup_win_rate),
                )
            ]
        elif personal and personal.games:
            local_parts = [
                self._text(
                    "hover.local.champion", champion=hover_name,
                    games=personal.games, rate=_fmt_rate(personal.win_rate),
                )
            ]
        else:
            local_parts = [self._text("hover.local.none")]
        if synergy and synergy.win_rate is not None:
            adc = allied_adc_member(self.draft)
            if adc:
                local_parts.append(
                    self._text(
                        "hover.local.synergy",
                        adc=self._champion_text(adc.champion_id, adc.champion_name_ko),
                        support=hover_name, rate=_fmt_rate(synergy.win_rate),
                        games=self._games_text(synergy.games),
                    )
                )
        source_note = (
            self._text("hover.source.auto")
            if self.draft.selected_enemy_support_source == "AUTO_ENEMY_SUPPORT"
            else self._text("hover.source.manual")
        )
        local_parts.append(source_note)
        self.hover_matchup_local.configure(
            text="    |    ".join(local_parts), fg=COLORS["muted"]
        )

    def _render_synergy(self) -> None:
        if not self._selection_panel_needs_render(
            "synergy", self.draft.my_role, self.draft.unavailable_champions(),
            allied_adc_member(self.draft), self.opgg_synergy_snapshot,
            self._synergy_refreshing, tuple(sorted(
                (champion_id, repr(stat))
                for champion_id, stat in self._personal_stats_cache.items()
            )),
        ):
            return
        for item in self.synergy_tree.get_children():
            self.synergy_tree.delete(item)
        if self.draft.my_role != "SUPPORT":
            self.synergy_summary_label.configure(
                text="서포터 포지션일 때 아군 원딜과의 조합 통계를 자동으로 표시합니다.",
                fg=COLORS["muted"],
            )
            self.synergy_flow_label.configure(text="서포터 전용")
            return
        adc = allied_adc_member(self.draft)
        if not adc:
            self.synergy_summary_label.configure(
                text="아군 원딜이 확정되거나 HOVER하면 OP.GG 조합 승률을 자동으로 가져옵니다.",
                fg=COLORS["muted"],
            )
            self.synergy_flow_label.configure(text="원딜 선택 대기")
            return
        state_text = "확정 픽" if adc.state == "LOCKED" else "픽 의사 · 변경 가능"
        self.synergy_flow_label.configure(
            text=f"조합 해석 · {adc_flow_hint(adc.champion_id)}"
        )
        snapshot = self.opgg_synergy_snapshot
        if not snapshot or snapshot.ally_champion_id != adc.champion_id:
            status = "OP.GG MCP 조합 통계 요청 중…" if self._synergy_refreshing else (
                "조합 캐시 없음 · 자동 갱신 대기"
            )
            self.synergy_summary_label.configure(
                text=f"{adc.champion_name_ko} · {state_text} · {status}",
                fg=COLORS["blue"] if self._synergy_refreshing else COLORS["orange"],
            )
            return

        fetched = snapshot.fetched_at.replace("T", " ")[:16]
        stale = not self._synergy_snapshot_fresh(snapshot)
        suffix = " · 새 데이터 확인 중" if self._synergy_refreshing else (
            " · 오래된 캐시" if stale else ""
        )
        self.synergy_summary_label.configure(
            text=(
                f"{adc.champion_name_ko} · {state_text} · OP.GG MCP 솔로랭크 조합 "
                f"{len(snapshot.synergies)}개 · {fetched}{suffix}"
            ),
            fg=COLORS["orange"] if stale else COLORS["green"],
        )
        local_stats = self._personal_stats_for(
            [item.champion_id for item in snapshot.synergies]
        )
        unavailable = set(self.draft.unavailable_champions())
        for item in snapshot.synergies[:10]:
            personal = local_stats.get(item.champion_id)
            local_text = (
                f"{personal.ally_adc_wins}승 {personal.ally_adc_losses}패 · "
                f"{_fmt_rate(personal.ally_adc_win_rate)}"
                if personal and personal.ally_adc_games
                else "계산 중…" if personal is None and self.storage.get_setting("riot_puuid")
                else "내 기록 없음"
            )
            blocked = item.champion_id in unavailable
            status = (
                "밴/픽 제외" if blocked else
                "강력 후보" if item.synergy_rank <= 3 else
                "조합 후보"
            )
            icon = self.icon_cache.get(
                item.champion_id, 32, self._selection_icon_ready("synergy")
            )
            tag = "blocked" if blocked else (
                "best" if item.synergy_rank <= 3 else "good"
            )
            self.synergy_tree.insert(
                "", "end", text=f"  {item.champion_name_ko}", image=icon or "",
                values=(
                    f"{item.synergy_rank}위",
                    _fmt_rate(item.win_rate),
                    _fmt_games(item.games),
                    local_text,
                    synergy_tier_label(item.synergy_tier),
                    status,
                ),
                tags=(tag,),
            )

    def _render_opgg_meta(self) -> None:
        if not self._selection_panel_needs_render(
            "meta", self.draft.my_role, self.draft.unavailable_champions(),
            self.opgg_meta_snapshot,
            self._data_preference("opgg_meta_display_count"),
            tuple(sorted(
                (champion_id, repr(stat))
                for champion_id, stat in self._personal_stats_cache.items()
            )),
        ):
            return
        for item in self.opgg_meta_tree.get_children():
            self.opgg_meta_tree.delete(item)
        snapshot = self.opgg_meta_snapshot
        role_name = position_name(self.draft.my_role)
        if not snapshot:
            self.opgg_meta_summary_label.configure(
                text=(
                    f"{role_name} 순위 캐시가 없습니다. OP.GG 데이터 갱신을 누르면 "
                    "상대 상성과 함께 받아옵니다."
                )
            )
            return
        if not any(entry.position_rank for entry in snapshot.counters):
            self.opgg_meta_summary_label.configure(
                text=(
                    "이전 형식의 OP.GG 캐시에는 종합 순위·픽률·밴률이 없습니다. "
                    "갱신 쿨타임이 끝나면 OP.GG 순위+상성 갱신을 눌러 주세요."
                )
            )
            return
        stamp = snapshot.updated_at.replace("T", " ")[:16]
        display_count = self._data_preference("opgg_meta_display_count")
        self.opgg_meta_tree.configure(height=display_count)
        self.opgg_meta_summary_label.configure(
            text=(
                f"{role_name} · 패치 {snapshot.patch} · {snapshot.region} · {snapshot.tier} · "
                f"{stamp} 갱신    OP.GG 상위 {display_count}개 (승률·픽률·밴률 구분)"
            )
        )
        entries = list(snapshot.counters[:display_count])
        puuid = self.storage.get_setting("riot_puuid")
        personal_stats = self._personal_stats_for(
            [entry.champion_id for entry in entries]
        ) if puuid else {}
        unavailable = set(self.draft.unavailable_champions())
        for index, entry in enumerate(entries, start=1):
            rank = entry.position_rank or index
            personal = personal_stats.get(entry.champion_id)
            personal_text = (
                f"{personal.wins}승{personal.losses}패 · {_fmt_rate(personal.win_rate)}"
                if personal and personal.games
                else ("계산 중..." if puuid and personal is None else "기록 없음")
            )
            blocked = entry.champion_id in unavailable
            status = "밴/픽 제외" if blocked else ("최상위 메타" if rank <= 3 else "선택 가능")
            icon = self.icon_cache.get(
                entry.champion_id, 32, self._selection_icon_ready("meta")
            )
            tag = "blocked" if blocked else ("top3" if rank <= 3 else "top10")
            self.opgg_meta_tree.insert(
                "", "end", text=f"  {entry.champion_name_ko}", image=icon or "",
                values=(
                    f"{rank}위", _fmt_rate(entry.overall_win_rate),
                    _fmt_rate(entry.pick_rate), _fmt_rate(entry.ban_rate),
                    personal_text, status,
                ), tags=(tag,),
            )

    def _render_opgg(self) -> None:
        if not self._selection_panel_needs_render(
            "opgg", self.draft.my_role, self.draft.unavailable_champions(),
            self.draft.selected_enemy_support_id, self.opgg_snapshot,
            self.opgg_synergy_snapshot, self._support_filter,
            tuple(sorted(
                (champion_id, repr(stat))
                for champion_id, stat in self._personal_stats_cache.items()
            )),
        ):
            return
        for tree in (
            self.counter_tree, self.personal_counter_tree,
            self.matchup_counter_tree,
        ):
            for item in tree.get_children():
                tree.delete(item)
        self._clear(self.weak_frame)
        snapshot = self.opgg_snapshot
        self._refresh_filter_buttons()
        if not snapshot:
            self.opgg_summary_label.configure(
                text="캐시된 데이터가 없습니다. OP.GG 데이터 갱신 버튼을 눌러 현재 통계를 가져오세요."
            )
            self.copy_top3_button.configure(state="disabled")
            self.opgg_calc_label.configure(text="")
            return
        self.copy_top3_button.configure(state="normal")
        role_name = self._position_text(self.draft.my_role)
        if snapshot.enemy_support_id:
            self.counter_tree.heading("winrate", text=self._tr("OP.GG 상대 승률"))
            summary = self._text(
                "details.matchup.summary",
                champion=self._champion_text(
                    snapshot.enemy_support_id, snapshot.enemy_support_name_ko,
                ),
                role=role_name, patch=snapshot.patch, region=snapshot.region,
                tier=snapshot.tier,
                win_rate=_fmt_rate(snapshot.target_overall_win_rate),
                pick_rate=_fmt_rate(snapshot.target_pick_rate),
                ban_rate=_fmt_rate(snapshot.target_ban_rate),
            )
        else:
            self.counter_tree.heading("winrate", text=self._tr("OP.GG 전체 승률"))
            summary = self._text(
                "details.blind.summary", role=role_name, patch=snapshot.patch,
                tier=f"{snapshot.region} · {snapshot.tier}",
            )
        self.opgg_summary_label.configure(text=summary)
        puuid = self.storage.get_setting("riot_puuid")
        unavailable = set(self.draft.unavailable_champions())
        counters = self._filtered_counters()
        personal_stats = self._personal_stats_for(
            [counter.champion_id for counter in counters]
        ) if puuid else {}
        if not counters:
            self.opgg_calc_label.configure(text="해당 유형 후보 없음", fg=COLORS["orange"])
        elif puuid and any(personal_stats.get(item.champion_id) is None for item in counters):
            self.opgg_calc_label.configure(text="내 전적 조합 계산 중…", fg=COLORS["blue"])
        else:
            self.opgg_calc_label.configure(
                text=self._text(
                    "details.calc.done",
                    filter=(
                        self._tr(SUPPORT_FILTER_LABELS[self._support_filter])
                        if self.draft.my_role == "SUPPORT"
                        else f"{role_name} {self._tr('전체')}"
                    ),
                    count=len(counters),
                ),
                fg=COLORS["muted"],
            )
        for index, counter in enumerate(counters, start=1):
            personal = personal_stats.get(counter.champion_id)
            synergy = self._synergy_for(counter.champion_id)
            score, confidence = candidate_score(counter, personal, synergy)
            status = (
                "밴/픽 제외" if counter.champion_id in unavailable else
                "원딜궁합 TOP3" if synergy and synergy.synergy_rank <= 3 else
                "상성 불리" if counter.versus_win_rate < 50 else "추천 가능"
            )
            icon = self.icon_cache.get(
                counter.champion_id, 32, self._selection_icon_ready("opgg")
            )
            self.counter_tree.insert(
                "", "end", text=f"  {counter.champion_name_ko}", image=icon or "", values=(
                    index, f"{score:.0f}", confidence, _fmt_rate(counter.versus_win_rate),
                    self._games_text(counter.games), status,
                ), tags=(("blocked",) if counter.champion_id in unavailable else
                         (("weak",) if counter.versus_win_rate < 50 else
                          (("strong",) if score >= 60 else (("good",) if score >= 53 else ()))))
            )
            row_tag = (
                "blocked" if counter.champion_id in unavailable else
                "strong" if personal and personal.games and (personal.win_rate or 0) >= 55 else
                "weak" if personal and personal.games and (personal.win_rate or 0) < 45 else
                "good"
            )
            if personal is None and puuid:
                personal_values = ("--", "계산 중", "--", "--", "--", "로컬 계산 중")
                matchup_values = (
                    "--", "계산 중", "--", "--",
                    _fmt_rate(counter.versus_win_rate), "로컬 계산 중",
                )
            elif personal and personal.games:
                personal_values = (
                    f"{personal.games}판", f"{personal.wins}승 / {personal.losses}패",
                    _fmt_rate(personal.win_rate),
                    f"{personal.kda:.2f}" if personal.kda is not None else "--",
                    f"{personal.vision_score:.1f}" if personal.vision_score is not None else "--",
                    "저장된 솔로랭크",
                )
                matchup_values = (
                    f"{personal.matchup_games}판" if personal.matchup_games else "0판",
                    (
                        f"{personal.matchup_wins}승 / {personal.matchup_losses}패"
                        if personal.matchup_games else "기록 없음"
                    ),
                    _fmt_rate(personal.matchup_win_rate),
                    personal.matchup_confidence,
                    _fmt_rate(counter.versus_win_rate),
                    "맞상대 기록 있음" if personal.matchup_games else "맞상대 기록 없음",
                )
            else:
                personal_values = ("0판", "기록 없음", "--", "--", "--", "챔피언 기록 없음")
                matchup_values = (
                    "0판", "기록 없음", "--", "데이터 없음",
                    _fmt_rate(counter.versus_win_rate), "맞상대 기록 없음",
                )
            self.personal_counter_tree.insert(
                "", "end", text=f"  {counter.champion_name_ko}", image=icon or "",
                values=personal_values, tags=(row_tag,),
            )
            matchup_tag = (
                "blocked" if counter.champion_id in unavailable else
                "strong" if personal and personal.matchup_games
                and (personal.matchup_win_rate or 0) >= 55 else
                "weak" if personal and personal.matchup_games
                and (personal.matchup_win_rate or 0) < 45 else "good"
            )
            self.matchup_counter_tree.insert(
                "", "end", text=f"  {counter.champion_name_ko}", image=icon or "",
                values=matchup_values, tags=(matchup_tag,),
            )
        if snapshot.weak_picks:
            tk.Label(
                self.weak_frame, text="상성이 불리한 픽", bg=COLORS["panel"], fg=COLORS["red"],
                font=("Malgun Gothic", 9, "bold"),
            ).pack(side="left", padx=(0, 8))
            for item in snapshot.weak_picks[:5]:
                self._chip(
                    self.weak_frame, f"{item.champion_name_ko} {_fmt_rate(item.versus_win_rate)}",
                    COLORS["red"], champion_id=item.champion_id,
                )

    def _filtered_counters(self) -> list[OpggCounter]:
        if not self.opgg_snapshot:
            return []
        if self.draft.my_role != "SUPPORT" or self._support_filter == "ALL":
            return self.opgg_snapshot.counters[:10]
        # Include unfavorable rows for a selected archetype as well. Otherwise
        # a type can look empty merely because none of its champions made the
        # overall top-ten matchup slice.
        combined: dict[str, OpggCounter] = {}
        for counter in self.opgg_snapshot.counters + self.opgg_snapshot.weak_picks:
            combined.setdefault(counter.champion_id, counter)
        return sorted(
            (
                counter for counter in combined.values()
                if support_archetype(counter.champion_id) == self._support_filter
            ),
            key=lambda counter: (counter.versus_win_rate, counter.games),
            reverse=True,
        )[:10]

    def _set_support_filter(self, support_filter: str) -> None:
        if support_filter not in SUPPORT_FILTER_LABELS:
            return
        self._support_filter = (
            support_filter if self.draft.my_role == "SUPPORT" else "ALL"
        )
        self._render_opgg()

    def _refresh_filter_buttons(self) -> None:
        support_mode = self.draft.my_role == "SUPPORT"
        if not support_mode:
            self._support_filter = "ALL"
        self.position_filter_label.configure(
            text=(
                self._tr("플레이 유형") if support_mode
                else f"{self._tr('추천 포지션')} · {self._position_text(self.draft.my_role)}"
            )
        )
        for key, button in self.support_filter_buttons.items():
            if key != "ALL" and not support_mode:
                button.pack_forget()
                continue
            if not button.winfo_manager():
                button.pack(side="left", padx=(0, 5))
            button.configure(
                text=(self._position_text(self.draft.my_role) + " " + self._tr("전체"))
                if key == "ALL" and not support_mode else self._tr(SUPPORT_FILTER_LABELS[key])
            )
            selected = key == self._support_filter
            self._set_button_selected(button, selected, COLORS["blue"])

    def _copy_top3_candidates(self) -> None:
        counters = [
            counter for counter in self._filtered_counters()
            if counter.champion_id not in set(self.draft.unavailable_champions())
        ]
        if not counters:
            self.opgg_calc_label.configure(text="복사할 추천 가능 후보가 없습니다.", fg=COLORS["orange"])
            return
        puuid = self.storage.get_setting("riot_puuid")
        personal_stats = self._personal_stats_for(
            [counter.champion_id for counter in counters]
        ) if puuid else {}
        ranked = sorted(
            counters,
            key=lambda counter: candidate_score(
                counter, personal_stats.get(counter.champion_id),
                self._synergy_for(counter.champion_id),
            )[0],
            reverse=True,
        )[:3]
        role_name = position_name(self.draft.my_role)
        filter_name = (
            SUPPORT_FILTER_LABELS[self._support_filter]
            if self.draft.my_role == "SUPPORT" else f"{role_name} 전체"
        )
        lines = [
            f"내 포지션: {role_name} · 적 {role_name}: "
            f"{self.draft.selected_enemy_support_name_ko or '블라인드'} · 유형: {filter_name}"
        ]
        for index, counter in enumerate(ranked, start=1):
            personal = personal_stats.get(counter.champion_id)
            synergy = self._synergy_for(counter.champion_id)
            score, confidence = candidate_score(counter, personal, synergy)
            local = (
                f"내 전적 {personal.games}판 {_fmt_rate(personal.win_rate)}"
                if personal and personal.games else "내 전적 없음"
            )
            lines.append(
                f"{index}. {counter.champion_name_ko} · 종합 {score:.0f} · 신뢰도 {confidence} · "
                f"상대 OP.GG {_fmt_rate(counter.versus_win_rate)} ({_fmt_games(counter.games)}) · "
                f"원딜 조합 OP.GG "
                f"{_fmt_rate(synergy.win_rate) if synergy else '미확인'} "
                f"({ _fmt_games(synergy.games) if synergy else '표본 없음'}) · "
                f"내 원딜 조합 "
                f"{_fmt_rate(personal.ally_adc_win_rate) if personal and personal.ally_adc_games else '기록 없음'} · "
                f"{local}"
            )
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.opgg_calc_label.configure(text="TOP 3 후보를 복사했습니다.", fg=COLORS["gold"])

    def _render_prompt_summary(self) -> None:
        if not self._selection_panel_needs_render(
            "prompt", self.draft.payload(), self.opgg_snapshot,
            self.opgg_meta_snapshot, self.opgg_synergy_snapshot,
            self.recommendations, self.recommendation_snapshot_id,
            self._prompt_copied_snapshot_id,
            self.storage.get_setting("prompt_memory_version"),
            self.storage.get_setting("codex_thread_id"),
            self.storage.get_setting("codex_memory_thread_id"),
            self.storage.get_setting("codex_memory_version"),
            self._codex_cli_running, self._codex_cli_error,
            self._recommendation_apply_error,
            tuple(sorted(
                (champion_id, repr(stat))
                for champion_id, stat in self._personal_stats_cache.items()
            )),
        ):
            return
        target = (
            self._champion_text(
                self.draft.selected_enemy_support_id,
                self.draft.selected_enemy_support_name_ko,
            )
            if self.draft.selected_enemy_support_id
            else self._text("prompt.target.unknown")
        )
        role_name = self._position_text(self.draft.my_role)
        certainty = {
            "MANUAL_ENEMY_SUPPORT": self._text("prompt.certainty.manual"),
            "AUTO_ENEMY_SUPPORT": self._text("prompt.certainty.auto"),
            "MANUAL_UNKNOWN": self._text("prompt.certainty.unknown"),
        }.get(
            self.draft.selected_enemy_support_source,
            self._text("prompt.certainty.unknown"),
        )
        ally_locked = len(self.draft.ally_locked)
        ally_hover = len(self.draft.ally_hover) + int(self.draft.my_hover is not None)
        matchup = self._text(
            "prompt.matchup.ready"
            if self.opgg_snapshot and self.opgg_snapshot.enemy_support_id
            else "prompt.matchup.missing"
        )
        meta = self._text(
            "prompt.meta.ready" if self.opgg_meta_snapshot else "prompt.meta.missing"
        )
        adc = allied_adc_member(self.draft)
        synergy_ready = bool(
            adc and self.opgg_synergy_snapshot
            and self.opgg_synergy_snapshot.ally_champion_id == adc.champion_id
        )
        local_synergy_stats = self._local_synergy_stats_for_prompt()
        local_combo_count = sum(
            bool(stat and stat.ally_adc_games) for stat in local_synergy_stats.values()
        )
        codex_thread_id = self.storage.get_setting("codex_thread_id").strip()
        codex_memory_ready = bool(
            codex_thread_id
            and self.storage.get_setting("codex_memory_thread_id") == codex_thread_id
            and self.storage.get_setting("codex_memory_version") == MEMORY_PROMPT_VERSION
        )
        manual_memory_ready = (
            self.storage.get_setting("prompt_memory_version") == MEMORY_PROMPT_VERSION
        )
        memory_registered = codex_memory_ready or manual_memory_ready
        short_prompt_size = len(
            build_prompt(
                self.draft, self.opgg_snapshot, self.opgg_meta_snapshot,
                self.opgg_synergy_snapshot, local_synergy_stats,
                meta_limit=self._data_preference("opgg_meta_display_count"),
            )
        )
        self.prompt_summary_label.configure(
            text=self._text(
                "prompt.summary",
                memory=self._text(
                    "prompt.memory.cli" if codex_memory_ready else
                    "prompt.memory.manual" if manual_memory_ready else
                    "prompt.memory.required"
                ),
                size=short_prompt_size, role=role_name,
                locked=ally_locked + len(self.draft.enemy_locked),
                hover=ally_hover,
                bans=len(self.draft.ally_bans) + len(self.draft.enemy_bans),
                target=target, certainty=certainty, meta=meta, matchup=matchup,
                synergy=self._text(
                    "prompt.synergy.ready" if synergy_ready else "prompt.synergy.pending"
                ),
                local=local_combo_count, snapshot=self.draft.snapshot_id,
            )
        )
        stale = self._recommendations_stale()
        copied = self._prompt_copied_snapshot_id == self.draft.snapshot_id
        # A received answer remains the active answer until the next Codex
        # response replaces it. Draft changes are context warnings only.
        recommendation_ready = bool(
            self.recommendations
            and getattr(self, "recommendation_source", "") == "CODEX"
        )
        active_step = (
            0 if recommendation_ready else
            1 if not memory_registered else
            3
        )
        for index, (frame, badge, label) in enumerate(self.workflow_steps, start=1):
            complete = (
                (index == 1 and memory_registered)
                or index == 2
                or (index == 3 and (copied or recommendation_ready))
                or (index == 4 and recommendation_ready)
            )
            active = index == active_step and not recommendation_ready
            color = COLORS["green"] if complete else (
                COLORS["gold"] if active else COLORS["muted"]
            )
            background = "#17362e" if complete else (
                "#3a311f" if active else COLORS["panel_2"]
            )
            frame.configure(
                bg=background,
                highlightbackground=color if complete or active else COLORS["border"],
            )
            badge.configure(
                text="✓" if complete else str(index),
                bg=color if complete else COLORS["chip"],
                fg="#07101b" if complete else color,
            )
            label.configure(bg=background, fg=color)
        if self._codex_cli_running:
            status = self._text("prompt.exchange.running")
            status_color = COLORS["blue"]
        elif self._codex_cli_error:
            status = self._text("prompt.exchange.cli_error", error=self._codex_cli_error)
            status_color = COLORS["red"]
        elif self._recommendation_apply_error:
            status = self._text(
                "prompt.exchange.format_error", error=self._recommendation_apply_error,
            )
            status_color = COLORS["red"]
        elif not memory_registered:
            status = self._text("prompt.exchange.setup")
            status_color = COLORS["gold"]
        elif stale:
            status = self._text("prompt.exchange.stale")
            status_color = COLORS["orange"]
        elif recommendation_ready:
            status = self._text("prompt.exchange.ready")
            status_color = COLORS["green"]
        elif copied:
            status = self._text("prompt.exchange.copied")
            status_color = COLORS["blue"]
        elif codex_memory_ready:
            status = self._text("prompt.exchange.cli_ready")
            status_color = COLORS["green"]
        else:
            status = self._text("prompt.exchange.manual_ready")
            status_color = COLORS["gold"]
        self.exchange_status.configure(text=status, fg=status_color)
        if self._codex_cli_running:
            cli_status = self._text("prompt.cli.running")
            cli_color = COLORS["blue"]
        elif self._codex_cli_error:
            cli_status = self._text("prompt.cli.error", error=self._codex_cli_error)
            cli_color = COLORS["red"]
        elif self.codex_cli is None:
            cli_status = self._codex_cli_error or self._text("prompt.cli.missing")
            cli_color = COLORS["red"]
        elif codex_memory_ready:
            cli_status = self._text(
                "prompt.cli.ready", thread=codex_thread_id[:8],
            )
            cli_color = COLORS["green"]
        elif codex_thread_id:
            cli_status = self._text(
                "prompt.cli.thread", thread=codex_thread_id[:8],
            )
            cli_color = COLORS["orange"]
        else:
            cli_status = self._text("prompt.cli.setup")
            cli_color = COLORS["gold"]
        self.codex_cli_status_label.configure(text=cli_status, fg=cli_color)
        self.codex_recommend_button.configure(
            state=(
                "normal" if (
                    self.codex_recommendations_enabled
                    and not getattr(self, "demo", False)
                    and self.codex_cli is not None
                    and not self._codex_cli_running
                )
                else "disabled"
            ),
            text=(
                self._text("prompt.button.running") if self._codex_cli_running
                else self._text("prompt.button.recommend", role=role_name)
            ),
        )

    def _local_synergy_stats_for_prompt(
        self,
    ) -> dict[str, PersonalStat | None]:
        adc = allied_adc_member(self.draft)
        snapshot = self.opgg_synergy_snapshot
        if (
            self.draft.my_role != "SUPPORT" or not adc or not snapshot
            or snapshot.ally_champion_id != adc.champion_id
        ):
            return {}
        return self._personal_stats_for(
            [item.champion_id for item in snapshot.synergies]
        )

    def _recommendations_stale(self) -> bool:
        if not self.recommendations:
            return False
        if getattr(self, "recommendation_source", "") == "LOCAL":
            return False
        stored_context = getattr(self, "recommendation_context_signature", "")
        if stored_context:
            return stored_context != recommendation_draft_context_signature(self.draft)
        return self.recommendation_snapshot_id != self.draft.snapshot_id

    def _local_recommendation_candidates(self) -> list[OpggCounter]:
        snapshot = self.opgg_snapshot or self.opgg_meta_snapshot
        combined: dict[str, OpggCounter] = {}
        if snapshot:
            for counter in [*snapshot.counters, *snapshot.weak_picks]:
                combined.setdefault(counter.champion_id, counter)
        if combined:
            return list(combined.values())

        role = self.draft.my_role
        cached = getattr(self, "_local_fallback_candidates_by_role", {}).get(role)
        if cached is not None:
            return list(cached)

        catalog = self.storage.load_opgg_position_catalog(
            role, max_age=None,
        )
        champion_ids = list(catalog[1][:10]) if catalog else list(
            LOCAL_RECOMMENDATION_FALLBACKS.get(role, ())
        )
        candidates = [
            OpggCounter(
                champion_id=champion_id,
                champion_name_ko=self.registry.ko_name(champion_id),
                versus_win_rate=50.0,
                games=0,
                status="LOCAL_FALLBACK",
                position_rank=index,
            )
            for index, champion_id in enumerate(champion_ids, start=1)
            if self.registry.contains(champion_id)
        ]
        self._local_fallback_candidates_by_role[role] = list(candidates)
        return candidates

    def _refresh_local_recommendations(self) -> None:
        """Keep an instant local top three until a Codex answer replaces it."""
        if getattr(self, "recommendation_source", "") == "CODEX":
            return
        candidates = self._local_recommendation_candidates()
        unavailable = set(self.draft.unavailable_champions())
        adc = allied_adc_member(self.draft)
        context = repr((
            self.draft.my_role,
            self.draft.selected_enemy_support_id,
            adc.champion_id if adc else "",
            tuple(sorted(unavailable)),
            tuple(
                (
                    item.champion_id, item.versus_win_rate, item.games,
                    item.overall_win_rate, item.position_rank,
                )
                for item in candidates
            ),
        ))
        signature = sha256(context.encode("utf-8")).hexdigest()[:20]
        if (
            signature == getattr(self, "_local_recommendation_signature", "")
            and getattr(self, "recommendation_source", "") == "LOCAL"
        ):
            return

        candidate_ids = [item.champion_id for item in candidates]
        personal_stats = self._personal_stats_for(candidate_ids)
        synergies = {
            champion_id: self._synergy_for(champion_id)
            for champion_id in candidate_ids
        }
        recommendations = local_recommendations_from_candidates(
            candidates,
            unavailable=unavailable,
            personal_stats=personal_stats,
            synergies=synergies,
            enemy_name=(
                self._champion_text(
                    self.draft.selected_enemy_support_id,
                    self.draft.selected_enemy_support_name_ko,
                )
                if self.draft.selected_enemy_support_id else ""
            ),
            ally_adc_name=(
                self._champion_text(adc.champion_id, adc.champion_name_ko)
                if adc else ""
            ),
            role_name=self._position_text(self.draft.my_role),
            language=self.ui_language,
        )
        self._local_recommendation_signature = signature
        self.recommendation_source = "LOCAL"
        self.recommendations = recommendations
        self.recommendation_snapshot_id = self.draft.snapshot_id
        self.recommendation_context_signature = recommendation_draft_context_signature(
            self.draft
        )
        self.recommendation_enemy_support_id = (
            self.draft.selected_enemy_support_id or ""
        )
        self._recommendation_generation = (
            int(getattr(self, "_recommendation_generation", 0)) + 1
        )

    def _render_recommendations(self) -> None:
        self._refresh_local_recommendations()
        stale = self._recommendations_stale()
        current_target = self.draft.selected_enemy_support_id or ""
        recommendation_ids = [item.champion_id for item in self.recommendations]
        if not self._selection_panel_needs_render(
            "recommendations", getattr(self, "recommendation_source", ""),
            self.recommendation_snapshot_id,
            getattr(self, "recommendation_enemy_support_id", ""),
            current_target,
            self.recommendations, allied_adc_member(self.draft),
            self.opgg_snapshot, self.opgg_meta_snapshot,
            self.opgg_synergy_snapshot,
            tuple(
                (champion_id, repr(self._personal_stats_cache.get(champion_id)))
                for champion_id in recommendation_ids
            ),
        ):
            return
        self._clear(self.cards_frame)
        self._recommendation_action_buttons = []
        if not self.recommendations:
            self.champion_action_status.configure(
                text=self._text("recommendations.empty.status"),
                fg=COLORS["muted"],
            )
            tk.Label(
                self.cards_frame,
                text=self._text("recommendations.empty.body"),
                bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 10),
            ).pack(anchor="w", pady=12)
            return
        source = getattr(self, "recommendation_source", "")
        answered_target = getattr(self, "recommendation_enemy_support_id", "")
        target_changed = answered_target != current_target
        if source == "LOCAL":
            status_text = self._text(
                "recommendations.local_blind"
                if not current_target else "recommendations.local_matchup"
            )
            status_color = COLORS["blue"]
        elif target_changed:
            answered_name = (
                self._champion_text(answered_target)
                if answered_target else self._text("prompt.target.unknown")
            )
            current_name = (
                self._champion_text(current_target)
                if current_target else self._text("prompt.target.unknown")
            )
            status_text = self._text(
                "recommendations.fixed_target_changed",
                answered=answered_name, current=current_name,
            )
            status_color = COLORS["orange"]
        else:
            status_text = self._text("recommendations.fixed")
            status_color = COLORS["green"]
        self.champion_action_status.configure(
            text=status_text, fg=status_color,
        )
        puuid = self.storage.get_setting("riot_puuid")
        personal_stats = self._personal_stats_for(
            [recommendation.champion_id for recommendation in self.recommendations]
        ) if puuid else {}
        for column, recommendation in enumerate(self.recommendations):
            card = tk.Frame(self.cards_frame, bg=COLORS["border"], padx=1, pady=1)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 6))
            self.cards_frame.grid_columnconfigure(column, weight=1, uniform="cards")
            inner = tk.Frame(card, bg=COLORS["panel_2"], padx=14, pady=12)
            inner.pack(fill="both", expand=True)
            title_row = tk.Frame(inner, bg=COLORS["panel_2"])
            title_row.pack(fill="x")
            icon = self.icon_cache.get(
                recommendation.champion_id, 60,
                self._selection_icon_ready("recommendations"),
            )
            if icon:
                tk.Label(title_row, image=icon, bg=COLORS["panel_2"]).pack(side="left", padx=(0, 10))
            title_text = tk.Frame(title_row, bg=COLORS["panel_2"])
            title_text.pack(side="left", fill="x", expand=True)
            counter = self._counter_for(recommendation.champion_id)
            synergy = self._synergy_for(recommendation.champion_id)
            personal = personal_stats.get(recommendation.champion_id)
            tk.Label(
                title_text, text=f"{recommendation.rank}위  {recommendation.champion_name_ko}",
                bg=COLORS["panel_2"], fg=COLORS["gold"], font=("Malgun Gothic", 14, "bold"),
            ).pack(anchor="w")
            tk.Label(
                title_text, text=f"{recommendation.style} · 블라인드 안정성 {recommendation.blind_safety}",
                bg=COLORS["panel_2"], fg=COLORS["muted"], font=("Malgun Gothic", 9),
            ).pack(anchor="w")
            if counter:
                score, confidence = candidate_score(counter, personal, synergy)
                score_color = (
                    COLORS["green"] if score >= 60 else
                    COLORS["gold"] if score >= 53 else COLORS["muted"]
                )
                tk.Label(
                    title_text, text=f"종합 {score:.0f}점  ·  데이터 신뢰도 {confidence}",
                    bg=COLORS["panel_2"], fg=score_color,
                    font=("Malgun Gothic", 9, "bold"),
                ).pack(anchor="w", pady=(3, 10))
            else:
                tk.Label(
                    title_text, text="종합점수: OP.GG 후보표에 없는 추천",
                    bg=COLORS["panel_2"], fg=COLORS["muted"],
                    font=("Malgun Gothic", 8),
                ).pack(anchor="w", pady=(3, 10))
            self._stat_block(
                inner, "OP.GG 전체/상성", COLORS["blue"],
                f"상대 승률 {_fmt_rate(counter.versus_win_rate) if counter else '데이터 없음'}\n"
                f"표본 {_fmt_games(counter.games) if counter else '데이터 없음'}",
            )
            adc = allied_adc_member(self.draft)
            self._stat_block(
                inner, "우리 원딜 조합", COLORS["gold"],
                (
                    f"{adc.champion_name_ko if adc else '원딜 미확정'} + "
                    f"{recommendation.champion_name_ko}\n"
                    f"OP.GG {_fmt_rate(synergy.win_rate) if synergy else '상위 목록 밖/미확인'}"
                    f" · {_fmt_games(synergy.games) if synergy else '표본 없음'}\n"
                    f"내 로컬 "
                    f"{personal.ally_adc_wins}승 {personal.ally_adc_losses}패 · "
                    f"{_fmt_rate(personal.ally_adc_win_rate)}"
                    if adc and personal and personal.ally_adc_games
                    else (
                        f"{adc.champion_name_ko if adc else '원딜 미확정'} + "
                        f"{recommendation.champion_name_ko}\n"
                        f"OP.GG {_fmt_rate(synergy.win_rate) if synergy else '상위 목록 밖/미확인'}"
                        f" · {_fmt_games(synergy.games) if synergy else '표본 없음'}\n"
                        f"내 로컬 {'계산 중…' if puuid and personal is None else '기록 없음'}"
                    )
                ),
            )
            self._stat_block(
                inner, "내 챔피언 전적", COLORS["green"],
                (f"{personal.wins}승 {personal.losses}패 · {_fmt_rate(personal.win_rate)}\n"
                 f"KDA {personal.kda:.2f} · 시야 {personal.vision_score:.1f}"
                 if personal and personal.games
                 else ("계산 중..." if puuid and personal is None else "저장된 플레이 기록 없음")),
            )
            self._stat_block(
                inner, "내 상대 챔피언 전적", COLORS["purple"],
                (f"{personal.matchup_wins}승 {personal.matchup_losses}패 · "
                 f"{_fmt_rate(personal.matchup_win_rate)}\n{personal.matchup_confidence}"
                 if personal and personal.matchup_games
                 else ("계산 중..." if puuid and personal is None else "저장된 맞대결 기록 없음")),
            )
            self._paragraph(inner, "추천 이유", recommendation.reason)
            self._paragraph(inner, "팀 조합", recommendation.team_synergy)
            self._paragraph(inner, "라인전", recommendation.lane_plan)
            self._paragraph(inner, "주의", recommendation.watch_for, COLORS["orange"])
            action_row = tk.Frame(inner, bg=COLORS["panel_2"])
            action_row.pack(fill="x", pady=(10, 0))
            for action_column, (label, action, accent_key) in enumerate(
                RECOMMENDATION_ACTION_SPECS
            ):
                accent = COLORS[accent_key]
                button = self._button(
                    action_row,
                    label,
                    lambda champion_id=recommendation.champion_id, selected_action=action:
                        self._execute_champion_action(champion_id, selected_action),
                    accent,
                    filled=action == "pick",
                )
                button.grid(
                    row=0, column=action_column, sticky="ew",
                    padx=(0 if action_column == 0 else 4, 0),
                )
                button.configure(
                    state=(
                        "normal" if recommendation_action_available(
                            action,
                            enabled=not self._champion_action_running,
                            stale=stale,
                            demo=self.demo,
                        ) else "disabled"
                    )
                )
                self._recommendation_action_buttons.append((button, action))
                action_row.grid_columnconfigure(action_column, weight=1, uniform="champ_actions")

    def _set_recommendation_actions_enabled(self, enabled: bool) -> None:
        stale = self._recommendations_stale()
        for button, action in self._recommendation_action_buttons:
            state = (
                "normal" if recommendation_action_available(
                    action, enabled=enabled, stale=stale, demo=self.demo,
                ) else "disabled"
            )
            try:
                if button.winfo_exists():
                    button.configure(state=state)
            except tk.TclError:
                continue

    def _execute_champion_action(
        self,
        champion_id: str,
        action: str,
        *,
        quiet: bool = False,
        expected_current_champion_ids: set[int] | None = None,
    ) -> None:
        if self.demo or self._champion_action_running:
            return
        champion = self.registry.by_id.get(champion_id)
        if not champion or int(champion[0]) <= 0:
            messagebox.showerror(
                "챔피언 확인 실패",
                "챔피언 ID를 확인하지 못했습니다. 롤 클라이언트에서 직접 선택해 주세요.",
                parent=self.root,
            )
            return
        champion_key, champion_name = int(champion[0]), champion[1]
        labels = {
            "hover": "롤에 선택", "pick": "픽 확정", "ban": "밴 확정",
        }
        action_label = labels.get(action, action)
        self._champion_action_running = True
        self.champion_action_status.configure(
            text=f"{champion_name} {action_label} 전 검사 중 · 보유/밴/픽 순서 확인",
            fg=COLORS["blue"],
        )
        self._set_recommendation_actions_enabled(False)

        def success(_result: object) -> None:
            self._champion_action_running = False
            checks = (
                "픽 확정 아님 · 롤에서 그대로 바꾸거나 확정 가능"
                if action == "hover" else (
                    "밴 가능·현재 밴 순서 확인 완료"
                    if action == "ban" else
                    "보유·밴·현재 픽 순서 확인 완료"
                )
            )
            self.champion_action_status.configure(
                text=f"{champion_name} {action_label} 완료 · {checks}",
                fg=COLORS["green"],
            )
            self._set_recommendation_actions_enabled(True)
            self.root.after(80, self._poll_lcu)

        def error(exc: Exception) -> None:
            self._champion_action_running = False
            guidance = f"{exc} 롤 클라이언트에서 직접 선택해 주세요."
            self.champion_action_status.configure(text=guidance, fg=COLORS["red"])
            self._set_recommendation_actions_enabled(True)
            if not quiet:
                messagebox.showwarning(
                    f"{champion_name} {action_label} 실패",
                    guidance,
                    parent=self.root,
                )

        self._background(
            lambda: self.lcu.perform_champion_action(
                champion_key,
                action,
                expected_current_champion_ids=expected_current_champion_ids,
            ),
            success,
            error,
        )

    def _play_card_state_signature(self) -> str:
        """Return a stable signature for only the data visible inside player cards.

        Game time is deliberately excluded so the three-second live poll can update
        the header without destroying and recreating all ten cards.
        """
        players = tuple(
            (
                player.riot_id, player.champion_id, player.champion_name_ko,
                player.team, player.position, player.level, player.is_active_player,
                player.draft_pick_turn, player.draft_team_pick_order,
            )
            for player in self.live_game.players
        )
        profiles = tuple(
            (player.riot_id, repr(self.player_profiles.get(player.riot_id)))
            for player in self.live_game.players
        )
        duo_pairs = tuple(
            (riot_id, tuple(sorted(values)))
            for riot_id, values in sorted(self.duo_pairs.items())
        )
        return repr((
            self.live_game.active_team, players, profiles, duo_pairs,
            tuple(sorted(getattr(self, "lane_matchups", {}).items())),
            getattr(self, "_duo_checking", False),
            getattr(self, "_duo_checked_signature", ""),
        ))

    def _single_play_card_signature(self, player: LivePlayer) -> str:
        position = self._comparable_live_position(player.position)
        live_game = getattr(self, "live_game", None)
        roster = getattr(live_game, "players", [player])
        duo_visual = duo_group_visuals(
            roster, self.duo_pairs,
            active_team=getattr(live_game, "active_team", None),
        ).get(player.riot_id)
        return repr((
            player.riot_id, player.champion_id, player.champion_name_ko,
            player.team, player.position, player.level, player.is_active_player,
            player.draft_pick_turn, player.draft_team_pick_order,
            self._player_profile_render_value(
                self.player_profiles.get(player.riot_id)
            ),
            tuple(sorted(self.duo_pairs.get(player.riot_id, []))),
            duo_visual,
            getattr(self, "lane_matchups", {}).get(position),
            getattr(self, "jungle_tendencies", {}).get(player.riot_id),
            getattr(self, "player_behaviors", {}).get(player.riot_id),
        ))

    @staticmethod
    def _player_profile_render_value(
        profile: PlayerProfileStat | None,
    ) -> PlayerProfileStat | None:
        """Ignore cache timestamps that are not rendered anywhere on a card."""
        return replace(profile, updated_at="") if profile is not None else None

    @staticmethod
    def _short_duo_name(riot_id: str, limit: int = 14) -> str:
        game_name = riot_id.split("#", 1)[0].strip() or riot_id
        return game_name if len(game_name) <= limit else game_name[:limit - 1] + "…"

    def _render_duo_legend(self) -> None:
        frame = getattr(self, "live_duo_legend", None)
        if frame is None:
            return
        visuals = duo_group_visuals(
            self.live_game.players, self.duo_pairs,
            active_team=self.live_game.active_team,
        )
        groups: dict[str, tuple[str, str, str, str, str]] = {}
        for player in self.live_game.players:
            visual = visuals.get(player.riot_id)
            if not visual:
                continue
            group, color, partner, level, evidence = visual
            first, second = sorted(
                (player.riot_id, partner), key=str.casefold,
            )
            groups.setdefault(
                group,
                (color, first, second, level, evidence),
            )
        signature = repr(tuple(sorted(groups.items())))
        if signature == getattr(self, "_play_duo_legend_signature", ""):
            return
        self._play_duo_legend_signature = signature
        self._clear(frame)
        if not groups:
            return

        for column in range(3):
            frame.grid_columnconfigure(column, weight=1, uniform="duo_legend")
        for index, (group, values) in enumerate(sorted(groups.items())):
            color, first, second, level, evidence = values
            outer = tk.Frame(frame, bg=color, padx=1, pady=1)
            outer.grid(
                row=index // 3, column=index % 3, sticky="ew",
                padx=(0 if index % 3 == 0 else 5, 5), pady=2,
            )
            chip = tk.Label(
                outer,
                text=(
                    f"●● 듀오 {group}  "
                    f"{self._short_duo_name(first)} ↔ {self._short_duo_name(second)}"
                    f"  · {level}"
                ),
                bg=COLORS["surface"], fg=color, padx=8, pady=4,
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            )
            chip.pack(fill="x")
            _HoverTooltip(
                chip,
                lambda group=group, first=first, second=second, level=level,
                evidence=evidence: (
                    f"듀오 {group}\n{first} ↔ {second}\n"
                    f"신뢰도: {level}\n근거: {evidence}"
                ),
            )

    def _render_duo_group_strip(
        self, parent: tk.Widget, visual: DuoVisual | None,
    ) -> None:
        if not visual:
            return
        group, color, partner, level, evidence = visual
        partner_short = self._short_duo_name(partner, 16)
        label = tk.Label(
            parent,
            text=f"●● 듀오 {group} ↔ {partner_short} · {level}",
            bg=color, fg="#07101b", padx=7, pady=3,
            font=("Malgun Gothic", 7, "bold"), anchor="w",
        )
        label.pack(fill="x", pady=(0, 6))
        _HoverTooltip(
            label,
            lambda group=group, partner=partner, level=level, evidence=evidence: (
                f"듀오 {group} 파트너: {partner}\n"
                f"신뢰도: {level}\n근거: {evidence}"
            ),
        )

    def _render_play(self) -> None:
        if self._current_main_tab_index() != 1:
            return
        self._render_previous_play_button()
        self._render_play_summary()
        self._render_play_prediction()
        self._render_duo_legend()
        if not self.live_game.players:
            self.live_game_label.configure(
                text=self._tr(
                    "현재 게임 없음 · 이전 게임 플레이탭을 다시 볼 수 있습니다."
                    if getattr(self, "_previous_play_state", None)
                    else "게임이 시작되면 자동으로 플레이 탭으로 이동합니다."
                )
            )
            self.live_profile_status.configure(text="")
            self.live_duo_status.configure(
                text=self._tr("DUO: 게임 시작 후 현재 10명의 최근 100경기 교집합을 확인합니다."),
                fg=COLORS["orange"],
            )
            if self._play_roster_signature == "EMPTY":
                return
            self._play_roster_signature = "EMPTY"
            self._play_card_signatures.clear()
            self._clear(self.live_allies_frame)
            self._clear(self.live_enemies_frame)
            for frame in (self.live_allies_frame, self.live_enemies_frame):
                tk.Label(
                    frame, text=self._tr("플레이어 데이터 대기 중"), bg=COLORS["panel_2"], fg=COLORS["muted"],
                    padx=14, pady=20, font=("Malgun Gothic", 9),
                ).grid(row=0, column=0, columnspan=5, sticky="ew", pady=3)
            self._schedule_play_insight_render()
            return
        self._render_live_game_clock()
        complete = sum(
            1 for profile in self.player_profiles.values()
            if profile.status in {"OK", "LOCAL_ONLY"}
        )
        visible = sum(
            1 for profile in self.player_profiles.values()
            if profile.status in {"OK", "LOCAL_ONLY", "PARTIAL"}
        )
        opgg_champion_ready = sum(
            1 for player in self.live_game.players
            if not player.is_active_player
            and self.player_profiles.get(player.riot_id)
            and self.player_profiles[player.riot_id].champion_data_source
            in {"OPGG", "OPGG_NOT_LISTED"}
        )
        riot_champion_ready = sum(
            1 for player in self.live_game.players
            if not player.is_active_player
            and self.player_profiles.get(player.riot_id)
            and self.player_profiles[player.riot_id].champion_data_source == "RIOT_LIVE"
        )
        other_players = sum(not player.is_active_player for player in self.live_game.players)
        if self._profiles_loading or self._opgg_profiles_loading:
            self.live_profile_status.configure(
                text=(
                    f"기본 정보 {visible}/{len(self.live_game.players)} · "
                    f"OP.GG 현 챔프 {opgg_champion_ready}/{other_players}명"
                ),
                fg=COLORS["blue"],
            )
        else:
            fallback_text = (
                f" · Riot 폴백 {riot_champion_ready}명" if riot_champion_ready else ""
            )
            self.live_profile_status.configure(
                text=(
                    f"상세 {complete}/{len(self.live_game.players)}명 · "
                    f"OP.GG 현 챔프 {opgg_champion_ready}/{other_players}명"
                    f"{fallback_text}"
                ),
                fg=(
                    COLORS["orange"]
                    if self._opgg_profile_failures else COLORS["muted"]
                ),
            )
        position_order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4,
                          "SUPPORT": 4, "UNKNOWN": 9}
        allies = sorted(self.live_game.allies, key=lambda p: position_order.get(p.position, 9))
        enemies = sorted(self.live_game.enemies, key=lambda p: position_order.get(p.position, 9))
        roster_signature = repr((
            tuple(player.riot_id for player in allies),
            tuple(player.riot_id for player in enemies),
        ))
        if roster_signature != self._play_roster_signature:
            self._play_roster_signature = roster_signature
            self._play_card_signatures.clear()
            self._clear(self.live_allies_frame)
            self._clear(self.live_enemies_frame)

        desired_keys: set[str] = set()
        for ally, frame, team_players in (
            (True, self.live_allies_frame, allies),
            (False, self.live_enemies_frame, enemies),
        ):
            for slot, player in enumerate(team_players):
                key = f"{'A' if ally else 'E'}:{slot}"
                desired_keys.add(key)
                signature = self._single_play_card_signature(player)
                if self._play_card_signatures.get(key) == signature:
                    continue
                for child in frame.grid_slaves(row=0, column=slot):
                    child.destroy()
                self._render_player_card(frame, player, ally=ally, slot=slot)
                self._play_card_signatures[key] = signature
        for key in list(self._play_card_signatures):
            if key not in desired_keys:
                self._play_card_signatures.pop(key, None)
        self._schedule_play_insight_render()

    def _render_live_game_clock(self) -> None:
        """Update the inexpensive clock label without rebuilding play panels."""
        if not self.live_game.players or not hasattr(self, "live_game_label"):
            return
        minutes = int(self.live_game.game_time // 60)
        seconds = int(self.live_game.game_time % 60)
        self.live_game_label.configure(
            text=self._text(
                "play.previous_game_clock"
                if self._showing_previous_play else "play.game_clock",
                mode=self.live_game.game_mode or self._tr("게임"),
                minutes=minutes,
                seconds=seconds,
                count=len(self.live_game.players),
            )
        )

    def _render_previous_play_button(self) -> None:
        button = getattr(self, "previous_play_button", None)
        if not isinstance(button, tk.Button):
            return
        should_show = bool(
            getattr(self, "_previous_play_state", None)
            and not self.live_game.players
            and not self._showing_previous_play
        )
        if should_show:
            button.configure(
                text=self._tr("이전 게임 플레이탭 보기"), state="normal",
            )
            if not button.winfo_manager():
                button.pack(side="right", padx=(0, 10))
        elif button.winfo_manager():
            button.pack_forget()

    def _capture_previous_play_state(self) -> None:
        """Freeze the current play board before game-end state is cleared."""
        if not getattr(self, "live_game", LiveGameSnapshot()).players:
            return
        self._previous_play_state = deepcopy({
            "live_game": self.live_game,
            "player_profiles": self.player_profiles,
            "opgg_player_profiles": self.opgg_player_profiles,
            "duo_pairs": self.duo_pairs,
            "lane_matchups": self.lane_matchups,
            "jungle_tendencies": self.jungle_tendencies,
            "player_behaviors": getattr(self, "player_behaviors", {}),
            "live_prediction": self._live_prediction,
            "lane_opponent_personal_stat": self._lane_opponent_personal_stat,
            "lane_opponent_behavior": self._lane_opponent_behavior,
            "my_personal_stat": self._my_personal_stat,
            "my_behavior": self._my_behavior,
            "opgg_profile_failures": self._opgg_profile_failures,
        })
        self._showing_previous_play = False

    def _invalidate_play_view(self) -> None:
        """Invalidate display signatures without starting any data loaders."""
        self._play_roster_signature = ""
        self._play_card_signatures.clear()
        self._play_summary_signature = ""
        self._play_prediction_signature = ()
        self._play_insight_signature = ""
        self._play_insight_section_signatures.clear()
        self._play_duo_legend_signature = ""

    def _clear_current_play_state(self) -> None:
        """Clear the active board while retaining the frozen previous board."""
        self.live_game = LiveGameSnapshot()
        self.player_profiles = {}
        self.opgg_player_profiles = {}
        self.duo_pairs = {}
        self.lane_matchups = {}
        self.jungle_tendencies = {}
        self.player_behaviors = {}
        self._live_prediction = None
        self._jungle_tendency_context = None
        self._lane_opponent_analysis_context = None
        self._lane_opponent_personal_stat = None
        self._lane_opponent_behavior = None
        self._my_account_analysis_context = None
        self._my_personal_stat = None
        self._my_behavior = None
        self._profiles_loading = False
        self._opgg_profiles_loading = False
        self._duo_checking = False
        self._opgg_profile_failures = 0
        self._duo_checked_signature = ""
        self._live_signature = ""
        self._live_active_signature = ""
        self._showing_previous_play = False
        self._invalidate_play_view()

    def _show_previous_play(self) -> None:
        """Restore the last board as a read-only view with no remote refresh."""
        state = getattr(self, "_previous_play_state", None)
        if not state or getattr(self, "game_phase", "None") in {
            "GameStart", "Reconnect", "InProgress",
        }:
            return
        restored = deepcopy(state)
        live_game = restored.get("live_game")
        if not isinstance(live_game, LiveGameSnapshot) or not live_game.players:
            return
        self.live_game = live_game
        self.player_profiles = dict(restored.get("player_profiles") or {})
        self.opgg_player_profiles = dict(restored.get("opgg_player_profiles") or {})
        self.duo_pairs = dict(restored.get("duo_pairs") or {})
        self.lane_matchups = dict(restored.get("lane_matchups") or {})
        self.jungle_tendencies = dict(restored.get("jungle_tendencies") or {})
        self.player_behaviors = dict(restored.get("player_behaviors") or {})
        prediction = restored.get("live_prediction")
        self._live_prediction = prediction if isinstance(prediction, GamePrediction) else None
        self._lane_opponent_personal_stat = restored.get("lane_opponent_personal_stat")
        self._lane_opponent_behavior = restored.get("lane_opponent_behavior")
        self._my_personal_stat = restored.get("my_personal_stat")
        self._my_behavior = restored.get("my_behavior")
        self._opgg_profile_failures = int(restored.get("opgg_profile_failures") or 0)
        self._profiles_loading = False
        self._opgg_profiles_loading = False
        self._duo_checking = False
        self._jungle_tendency_loading = False
        self._lane_opponent_analysis_loading = False
        self._my_account_analysis_loading = False
        # A distinct signature freezes this view against late callbacks from the
        # just-finished game while preserving every captured display value.
        previous_token = (
            self.live_game.active_riot_id
            or self.live_game.game_mode
            or f"last-{int(self.live_game.game_time)}"
        )
        self._live_signature = f"PREVIOUS:{previous_token}"
        self._live_active_signature = f"PREVIOUS:{previous_token}"
        self._showing_previous_play = True
        self._invalidate_play_view()
        self._render_play()

    def _ensure_jungle_tendencies(self) -> None:
        """Load detailed Riot evidence, then fall back to remote OP.GG form."""
        if (
            self.demo or getattr(self, "_showing_previous_play", False)
            or not self.live_game.players or self._jungle_tendency_loading
        ):
            return
        junglers = [
            player for player in self.live_game.players
            if self._comparable_live_position(player.position) == "JUNGLE"
        ]
        profile_keys = tuple(
            (
                player.riot_id,
                (self.player_profiles.get(player.riot_id) or PlayerProfileStat()).puuid,
                player.champion_id,
                int(self.registry.by_id.get(player.champion_id, (0, ""))[0]),
            )
            for player in junglers
        )
        opgg_profiles = {
            player.riot_id: self.opgg_player_profiles.get(player.riot_id)
            for player in junglers
        }
        opgg_signature = tuple(
            (
                riot_id,
                profile.fetched_at if profile else "",
                tuple(match.match_id for match in profile.recent_matches)
                if profile else (),
            )
            for riot_id, profile in sorted(opgg_profiles.items())
        )
        context: tuple[object, ...] = (
            self._live_signature, profile_keys, opgg_signature,
        )
        if not junglers or context == self._jungle_tendency_context:
            return
        self._jungle_tendency_context = context
        self._jungle_tendency_loading = True
        signature = self._live_signature

        def work() -> dict[str, JungleTendencyStat]:
            result: dict[str, JungleTendencyStat] = {}
            for riot_id, puuid, champion_id, champion_key in profile_keys:
                detailed: JungleTendencyStat | None = None
                if puuid:
                    detailed = self.storage.jungle_tendency(
                        puuid, champion_id, limit=30
                    )
                if detailed and detailed.status == "OK":
                    result[riot_id] = detailed
                    continue
                profile = opgg_profiles.get(riot_id)
                fallback = (
                    opgg_jungle_tendency(
                        profile, champion_key, champion_id, puuid,
                    )
                    if profile and profile.recent_matches_status == "OK"
                    else None
                )
                result[riot_id] = fallback or detailed or JungleTendencyStat(
                    puuid=puuid,
                    champion_id=champion_id,
                    message=(
                        "최근 솔로랭크 정글 표본 없음 · "
                        "상세 동선 분석은 Riot 경기 캐시 필요"
                    ),
                )
            return result

        def success(result: dict[str, JungleTendencyStat]) -> None:
            self._jungle_tendency_loading = False
            if signature != self._live_signature:
                self.root.after(50, self._ensure_jungle_tendencies)
                return
            self.jungle_tendencies = result
            self._play_insight_signature = ""
            self._schedule_play_render()
            # A second player identity may have arrived while the first local
            # scan was running. Its expanded context is picked up here.
            self.root.after(50, self._ensure_jungle_tendencies)

        def error(_exc: Exception) -> None:
            self._jungle_tendency_loading = False
            if signature == self._live_signature:
                self._play_insight_signature = ""
                self._schedule_play_render()

        self._background(work, success, error)

    def _ensure_lane_opponent_analysis(self) -> None:
        """Compute the opposing lane player's exact local head-to-head sample."""
        if (
            self.demo or getattr(self, "_showing_previous_play", False)
            or not self.live_game.players
            or self._lane_opponent_analysis_loading
        ):
            return
        active = next(
            (player for player in self.live_game.players if player.is_active_player),
            None,
        )
        if not active:
            return
        position = self._comparable_live_position(active.position)
        opponent = next(
            (
                player for player in self.live_game.enemies
                if self._comparable_live_position(player.position) == position
            ),
            None,
        )
        if not opponent:
            return
        profile = self.player_profiles.get(opponent.riot_id)
        opponent_puuid = profile.puuid if profile else ""
        active_signature = getattr(self, "_live_active_signature", "")
        context: tuple[object, ...] = (
            self._live_signature, active_signature,
            position, active.champion_id,
            opponent.riot_id, opponent_puuid, opponent.champion_id,
        )
        if not opponent_puuid or context == self._lane_opponent_analysis_context:
            return
        self._lane_opponent_analysis_context = context
        self._lane_opponent_analysis_loading = True
        signature = (self._live_signature, active_signature)

        def work() -> tuple[PersonalStat, PlayerBehaviorStat]:
            return (
                self.storage.personal_stat(
                    opponent_puuid,
                    opponent.champion_id,
                    active.champion_id,
                    limit=1000,
                    position=position,
                ),
                self.storage.player_behavior(
                    opponent_puuid,
                    opponent.champion_id,
                    position=position,
                    limit=20,
                ),
            )

        def success(result: tuple[PersonalStat, PlayerBehaviorStat]) -> None:
            self._lane_opponent_analysis_loading = False
            if signature != (
                self._live_signature,
                getattr(self, "_live_active_signature", ""),
            ):
                self.root.after(50, self._ensure_lane_opponent_analysis)
                return
            self._lane_opponent_personal_stat, self._lane_opponent_behavior = result
            self._play_insight_signature = ""
            self._schedule_play_render()

        def error(_exc: Exception) -> None:
            self._lane_opponent_analysis_loading = False
            if signature == (
                self._live_signature,
                getattr(self, "_live_active_signature", ""),
            ):
                self._play_insight_signature = ""
                self._schedule_play_render()

        self._background(work, success, error)

    def _ensure_my_account_analysis(self) -> None:
        """Compute my current-pick coaching sample without blocking live cards."""
        if (
            self.demo or getattr(self, "_showing_previous_play", False)
            or not self.live_game.players or self._my_account_analysis_loading
        ):
            return
        active = next(
            (player for player in self.live_game.players if player.is_active_player),
            None,
        )
        if not active:
            return
        position = self._comparable_live_position(active.position)
        opponent = next(
            (
                player for player in self.live_game.enemies
                if self._comparable_live_position(player.position) == position
            ),
            None,
        )
        ally_adc = next(
            (
                player for player in self.live_game.allies
                if self._comparable_live_position(player.position) == "BOTTOM"
            ),
            None,
        ) if position == "SUPPORT" else None
        profile = self.player_profiles.get(active.riot_id)
        my_puuid = (profile.puuid if profile else "") or self.storage.get_setting("riot_puuid")
        active_signature = getattr(self, "_live_active_signature", "")
        context: tuple[object, ...] = (
            self._live_signature, active_signature,
            position, active.champion_id, my_puuid,
            opponent.champion_id if opponent else "",
            ally_adc.champion_id if ally_adc else "",
        )
        if not my_puuid or context == self._my_account_analysis_context:
            return
        self._my_account_analysis_context = context
        self._my_account_analysis_loading = True
        signature = (self._live_signature, active_signature)

        def work() -> tuple[PersonalStat, PlayerBehaviorStat]:
            return (
                self.storage.personal_stat(
                    my_puuid,
                    active.champion_id,
                    opponent.champion_id if opponent else None,
                    ally_adc.champion_id if ally_adc else None,
                    limit=1000,
                    position=position,
                ),
                self.storage.player_behavior(
                    my_puuid, active.champion_id, position=position, limit=20,
                ),
            )

        def success(result: tuple[PersonalStat, PlayerBehaviorStat]) -> None:
            self._my_account_analysis_loading = False
            if signature != (
                self._live_signature,
                getattr(self, "_live_active_signature", ""),
            ):
                self.root.after(50, self._ensure_my_account_analysis)
                return
            self._my_personal_stat, self._my_behavior = result
            self._play_insight_signature = ""
            self._schedule_play_render()

        def error(_exc: Exception) -> None:
            self._my_account_analysis_loading = False
            if signature == (
                self._live_signature,
                getattr(self, "_live_active_signature", ""),
            ):
                self._play_insight_signature = ""
                self._schedule_play_render()

        self._background(work, success, error)

    def _render_play_insights(self) -> None:
        if not hasattr(self, "play_insight_sections"):
            return
        matchups = tuple(
            sorted(getattr(self, "lane_matchups", {}).items())
        )
        jungle_values = tuple(
            sorted(getattr(self, "jungle_tendencies", {}).items())
        )
        jungle_players = tuple(
            (
                player.riot_id, player.champion_id, player.champion_name_ko,
                player.team,
            )
            for player in self.live_game.players
            if self._comparable_live_position(player.position) == "JUNGLE"
        )
        active = next(
            (player for player in self.live_game.players if player.is_active_player),
            None,
        )
        lane_opponent = None
        if active:
            active_position = self._comparable_live_position(active.position)
            lane_opponent = next(
                (
                    player for player in self.live_game.enemies
                    if self._comparable_live_position(player.position)
                    == active_position
                ),
                None,
            )

        if not self.live_game.players:
            self.play_insight_status.configure(
                text=self._tr("게임 시작 후 자동 분석"), fg=COLORS["muted"]
            )
            empty_signature = repr((self._live_signature, "EMPTY"))
            if empty_signature != self._play_insight_signature:
                self._play_insight_signature = empty_signature
                self._play_insight_section_signatures.clear()
                for section in self.play_insight_sections.values():
                    self._clear(section)
                tk.Label(
                    self.play_insight_sections["lane"],
                    text=self._tr("현재 게임 정보 대기 중 · 플레이어 카드는 먼저 표시되고 분석은 뒤에서 채워집니다."),
                    bg=COLORS["surface"], fg=COLORS["muted"], padx=14, pady=16,
                    font=("Malgun Gothic", 9), anchor="w",
                ).pack(fill="x")
            return

        self._play_insight_signature = repr((self._live_signature, "ACTIVE"))
        ready = sum(stat.ally_win_rate is not None for _key, stat in matchups)
        self.play_insight_status.configure(
            text=(
                "정글 행동 계산 중…" if self._jungle_tendency_loading
                else f"게임 승률 {ready}/{len(matchups) or 5}라인 · 로컬 정글 분석"
            ),
            fg=COLORS["blue"] if self._jungle_tendency_loading else COLORS["muted"],
        )

        self._render_play_insight_section(
            "lane", repr((self._live_signature, matchups)),
            lambda parent: self._render_lane_insight_section(parent, matchups),
        )
        self._render_play_insight_section(
            "jungle_plan",
            repr((
                self._live_signature, matchups, jungle_values, jungle_players,
                self._jungle_tendency_loading,
            )),
            self._render_jungle_plan_section,
        )

        active_profile = self._player_profile_render_value(
            self.player_profiles.get(active.riot_id) if active else None
        )
        opponent_profile = (
            self._player_profile_render_value(
                self.player_profiles.get(lane_opponent.riot_id)
            ) if lane_opponent else None
        )
        active_position = (
            self._comparable_live_position(active.position) if active else "UNKNOWN"
        )
        active_matchup = dict(matchups).get(active_position)
        self._render_play_insight_section(
            "opponent",
            repr((
                self._live_signature,
                lane_opponent.riot_id if lane_opponent else "",
                opponent_profile, active_matchup,
                self._lane_opponent_personal_stat,
                self._lane_opponent_behavior,
                self._lane_opponent_analysis_loading,
            )),
            self._render_lane_opponent_account,
        )
        self._render_play_insight_section(
            "mine",
            repr((
                self._live_signature,
                active.riot_id if active else "",
                active_profile, active_matchup, jungle_values,
                self._my_personal_stat, self._my_behavior,
                self._lane_opponent_behavior,
                self._my_account_analysis_loading,
            )),
            self._render_my_account_coaching,
        )

    def _render_play_insight_section(
        self,
        key: str,
        signature: str,
        renderer: Callable[[tk.Widget], None],
    ) -> None:
        if self._play_insight_section_signatures.get(key) == signature:
            return
        self._play_insight_section_signatures[key] = signature
        section = self.play_insight_sections[key]
        self._clear(section)
        renderer(section)

    def _render_lane_insight_section(
        self,
        parent: tk.Widget,
        matchups: tuple[tuple[str, LaneMatchupStat], ...],
    ) -> None:
        matchup_row = tk.Frame(parent, bg=COLORS["panel"])
        matchup_row.pack(fill="x")
        for column in range(5):
            matchup_row.grid_columnconfigure(column, weight=1, uniform="lane_insights")
        position_order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "SUPPORT": 4}
        for position, stat in sorted(
            matchups, key=lambda item: position_order.get(item[0], 9)
        ):
            column = position_order.get(position, 0)
            border = tk.Frame(matchup_row, bg=COLORS["border"], padx=1, pady=1)
            border.grid(row=0, column=column, sticky="nsew", padx=3)
            card = tk.Frame(border, bg=COLORS["surface"], padx=10, pady=8)
            card.pack(fill="both", expand=True)
            tk.Label(
                card, text=ROLE_LABELS.get(position, position), bg=COLORS["chip"],
                fg=COLORS["blue"], padx=7, pady=2,
                font=("Malgun Gothic", 7, "bold"),
            ).pack(anchor="w")
            tk.Label(
                card,
                text=f"{stat.ally_champion_name_ko}  vs  {stat.enemy_champion_name_ko}",
                bg=COLORS["surface"], fg=COLORS["text"],
                font=("Malgun Gothic", 9, "bold"), anchor="w",
            ).pack(fill="x", pady=(6, 3))
            if stat.ally_win_rate is None:
                game_text, game_color = "게임 승률 · 표본 없음", COLORS["muted"]
            else:
                game_text = (
                    f"게임 승률  {stat.ally_win_rate:.1f}% : "
                    f"{(stat.enemy_win_rate or 0.0):.1f}%"
                )
                game_color = (
                    COLORS["green"] if stat.ally_win_rate >= 51.5
                    else COLORS["red"] if stat.ally_win_rate < 48.5
                    else COLORS["gold"]
                )
            tk.Label(
                card, text=game_text, bg=COLORS["surface"], fg=game_color,
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x")
            if stat.ally_laning_win_rate is None:
                lane_text, lane_color = "라인전 승률 · 소스 미제공", COLORS["muted"]
            else:
                lane_text = (
                    f"라인전 승률  {stat.ally_laning_win_rate:.1f}% : "
                    f"{(stat.enemy_laning_win_rate or 0.0):.1f}%"
                )
                lane_color = COLORS["purple"]
            tk.Label(
                card, text=lane_text, bg=COLORS["surface"], fg=lane_color,
                font=("Malgun Gothic", 7, "bold"), anchor="w",
            ).pack(fill="x", pady=(3, 0))
            detail = lane_matchup_label(stat.ally_win_rate)
            if stat.games:
                detail += f" · {stat.games:,}게임"
            tk.Label(
                card, text=detail, bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Malgun Gothic", 7), anchor="w",
            ).pack(fill="x", pady=(5, 0))
        self._render_bottom_lane_analysis(parent, dict(matchups))

    def _render_bottom_lane_analysis(
        self,
        parent: tk.Widget,
        matchups: dict[str, LaneMatchupStat],
    ) -> None:
        active = next(
            (
                player for player in self.live_game.players
                if player.is_active_player
            ),
            None,
        )
        if not active or self._comparable_live_position(active.position) not in {
            "BOTTOM", "SUPPORT",
        }:
            return

        def member(players: list[LivePlayer], position: str) -> LivePlayer | None:
            return next(
                (
                    player for player in players
                    if self._comparable_live_position(player.position) == position
                ),
                None,
            )

        ally_adc = member(self.live_game.allies, "BOTTOM")
        ally_support = member(self.live_game.allies, "SUPPORT")
        enemy_adc = member(self.live_game.enemies, "BOTTOM")
        enemy_support = member(self.live_game.enemies, "SUPPORT")
        if not all((ally_adc, ally_support, enemy_adc, enemy_support)):
            return
        assert ally_adc and ally_support and enemy_adc and enemy_support

        analysis = analyze_bottom_lane(
            ally_adc.champion_id,
            ally_support.champion_id,
            enemy_adc.champion_id,
            enemy_support.champion_id,
            ally_laning_win_rates=(
                matchups.get("BOTTOM").ally_laning_win_rate
                if matchups.get("BOTTOM") else None,
                matchups.get("SUPPORT").ally_laning_win_rate
                if matchups.get("SUPPORT") else None,
            ),
        )
        style_key = {
            "AGGRESSIVE": "bottom.style.aggressive",
            "SAFE": "bottom.style.safe",
            "EVEN": "bottom.style.even",
        }[analysis.style]
        style_color = {
            "AGGRESSIVE": COLORS["green"],
            "SAFE": COLORS["red"],
            "EVEN": COLORS["gold"],
        }[analysis.style]
        tip_key = {
            "AGGRESSIVE": "bottom.tip.aggressive",
            "SAFE": "bottom.tip.safe",
            "EVEN": "bottom.tip.even",
        }[analysis.style]
        confidence_key = {
            "HIGH": "bottom.confidence.high",
            "MEDIUM": "bottom.confidence.medium",
            "LOW": "bottom.confidence.low",
        }[analysis.confidence]
        timing_key = {
            "PRESS_ALL": "bottom.timing.press_all",
            "WAIT_LEVEL2": "bottom.timing.wait_level2",
            "WAIT_LEVEL3": "bottom.timing.wait_level3",
            "EARLY_THEN_SAFE": "bottom.timing.early_then_safe",
            "LEVEL6_TURN": "bottom.timing.level6_turn",
            "SAFE_ALL": "bottom.timing.safe_all",
            "EVEN_ALL": "bottom.timing.even_all",
            "MIXED": "bottom.timing.mixed",
        }[analysis.timing]

        border = tk.Frame(parent, bg=style_color, padx=1, pady=1)
        border.pack(fill="x", padx=3, pady=(10, 0))
        card = tk.Frame(border, bg=COLORS["surface"], padx=12, pady=10)
        card.pack(fill="x")
        heading = tk.Frame(card, bg=COLORS["surface"])
        heading.pack(fill="x")
        tk.Label(
            heading, text=self._text("bottom.title"),
            bg=COLORS["surface"], fg=COLORS["blue"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(side="left")
        tk.Label(
            heading, text=self._text(style_key),
            bg=style_color, fg="#07101b", padx=9, pady=3,
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="right")
        tk.Label(
            card,
            text=self._text(
                "bottom.composition",
                ally_adc=self._champion_text(
                    ally_adc.champion_id, ally_adc.champion_name_ko,
                ),
                ally_support=self._champion_text(
                    ally_support.champion_id, ally_support.champion_name_ko,
                ),
                enemy_adc=self._champion_text(
                    enemy_adc.champion_id, enemy_adc.champion_name_ko,
                ),
                enemy_support=self._champion_text(
                    enemy_support.champion_id, enemy_support.champion_name_ko,
                ),
            ),
            bg=COLORS["surface"], fg=COLORS["text"],
            font=("Malgun Gothic", 8, "bold"), anchor="w",
        ).pack(fill="x", pady=(7, 7))

        levels = tk.Frame(card, bg=COLORS["surface"])
        levels.pack(fill="x")
        level_colors = {
            "WIN": COLORS["green"], "LOSE": COLORS["red"],
            "EVEN": COLORS["gold"],
        }
        phase_keys = ("1", "2", "3", "6")
        for column, (phase_key, result) in enumerate(
            zip(phase_keys, analysis.level_results)
        ):
            levels.grid_columnconfigure(column, weight=1, uniform="bottom_levels")
            result_key = {
                "WIN": "bottom.phase_result.win",
                "LOSE": "bottom.phase_result.lose",
                "EVEN": "bottom.phase_result.even",
            }[result]
            tk.Label(
                levels, text=self._text(
                    result_key, phase=self._text(f"bottom.phase.{phase_key}"),
                ),
                bg=COLORS["panel_2"], fg=level_colors[result],
                padx=8, pady=5, font=("Malgun Gothic", 8, "bold"),
            ).grid(
                row=0, column=column, sticky="ew",
                padx=(0 if column == 0 else 4, 0),
            )

        tk.Label(
            card,
            text=(
                f"{self._text('bottom.how')} · {self._text(timing_key)}\n"
                f"{self._text(tip_key)}"
            ),
            bg=COLORS["surface"], fg=style_color,
            font=("Malgun Gothic", 8, "bold"), anchor="w",
        ).pack(fill="x", pady=(9, 3))
        step_lines = [
            (
                f"{self._text(f'bottom.phase.{phase_key}')} · "
                f"{self._text(f'bottom.step.{phase_key}.{result.lower()}')}"
            )
            for phase_key, result in zip(phase_keys, analysis.level_results)
        ]
        tk.Label(
            card, text="\n".join(step_lines),
            bg=COLORS["surface"], fg=COLORS["text"],
            font=("Malgun Gothic", 8), justify="left", anchor="w",
        ).pack(fill="x")
        tk.Label(
            card,
            text=(
                f"{self._text('bottom.item_note')}\n"
                f"{self._text(confidence_key)} · "
                f"{self._text('bottom.disclaimer')}"
            ),
            bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7), anchor="w",
        ).pack(fill="x", pady=(7, 0))

    def _render_jungle_plan_section(self, parent: tk.Widget) -> None:
        lower = tk.Frame(parent, bg=COLORS["panel"])
        lower.pack(fill="x", pady=(10, 0))
        lower.grid_columnconfigure(0, weight=1, uniform="jungle_insights")
        lower.grid_columnconfigure(1, weight=1, uniform="jungle_insights")
        lower.grid_columnconfigure(2, weight=2, uniform="jungle_insights")
        ordered_junglers = sorted(
            (
                player for player in self.live_game.players
                if self._comparable_live_position(player.position) == "JUNGLE"
            ),
            key=lambda player: player.team != self.live_game.active_team,
        )
        for column, player in enumerate(ordered_junglers[:2]):
            self._render_jungle_tendency_card(lower, player, column)
        self._render_play_plan_card(lower, 2)

    def _render_jungle_tendency_card(
        self, parent: tk.Widget, player: LivePlayer, column: int,
    ) -> None:
        ally = player.team == self.live_game.active_team
        accent = COLORS["green"] if ally else COLORS["red"]
        border = tk.Frame(parent, bg=accent, padx=1, pady=1)
        border.grid(row=0, column=column, sticky="nsew", padx=3)
        card = tk.Frame(border, bg=COLORS["surface"], padx=11, pady=9)
        card.pack(fill="both", expand=True)
        tk.Label(
            card,
            text=f"{'아군' if ally else '적군'} 정글 · {player.champion_name_ko}",
            bg=COLORS["surface"], fg=accent,
            font=("Malgun Gothic", 9, "bold"), anchor="w",
        ).pack(fill="x")
        stat = self.jungle_tendencies.get(player.riot_id)
        if not stat:
            text = "정글 표본 확인 중…" if self._jungle_tendency_loading else "최근 정글 표본 없음"
            tk.Label(
                card, text=text, bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Malgun Gothic", 8), anchor="w",
            ).pack(fill="x", pady=(8, 0))
            return
        if stat.status not in {"OK", "SUMMARY"}:
            tk.Label(
                card, text=stat.message or "행동 지표 미제공",
                bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Malgun Gothic", 8), anchor="w",
            ).pack(fill="x", pady=(8, 0))
            return
        if stat.status == "SUMMARY":
            badges = tk.Frame(card, bg=COLORS["surface"])
            badges.pack(fill="x", pady=(7, 5))
            for label in stat.labels[:3]:
                tk.Label(
                    badges, text=self._tr(label), bg=COLORS["chip"], fg=accent,
                    padx=6, pady=2, font=("Malgun Gothic", 7, "bold"),
                ).pack(side="left", padx=(0, 4))
            rate = f"{stat.win_rate:.1f}%" if stat.win_rate is not None else "--"
            kda = f"{stat.kda:.1f}" if stat.kda is not None else "--"
            tk.Label(
                card,
                text=(
                    f"최근 정글 {stat.games}경기 · {stat.wins}승 "
                    f"{stat.games - stat.wins}패 · {rate} · KDA {kda}"
                ),
                bg=COLORS["surface"], fg=COLORS["text"],
                font=("Malgun Gothic", 7, "bold"), anchor="w",
            ).pack(fill="x")
            scope = "현 챔프" if stat.champion_specific else "정글 전체"
            tk.Label(
                card,
                text=(
                    f"OP.GG 최근 솔로랭크 · {scope} · "
                    "상세 동선 지표는 Riot 캐시 필요"
                ),
                bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Malgun Gothic", 7), anchor="w",
            ).pack(fill="x", pady=(5, 0))
            self._render_jungle_advice(card, stat, ally)
            return
        badges = tk.Frame(card, bg=COLORS["surface"])
        badges.pack(fill="x", pady=(7, 5))
        for label in stat.labels[:3]:
            tk.Label(
                badges, text=self._tr(label), bg=COLORS["chip"], fg=accent,
                padx=6, pady=2, font=("Malgun Gothic", 7, "bold"),
            ).pack(side="left", padx=(0, 4))
        metrics: list[str] = []
        if stat.early_takedowns is not None:
            metrics.append(f"초반 관여 {stat.early_takedowns:.1f}")
        if stat.early_lane_kills is not None:
            metrics.append(f"초반 라인 처치 {stat.early_lane_kills:.1f}")
        if stat.jungle_cs_10 is not None:
            metrics.append(f"10분 정글 CS {stat.jungle_cs_10:.0f}")
        if stat.enemy_jungle_cs is not None:
            metrics.append(f"적 정글 몬스터 {stat.enemy_jungle_cs:.1f}")
        tk.Label(
            card, text=" · ".join(metrics[:3]), bg=COLORS["surface"],
            fg=COLORS["text"], font=("Malgun Gothic", 7, "bold"),
            anchor="w", justify="left", wraplength=320,
        ).pack(fill="x")
        scope = "현 챔프" if stat.champion_specific else "정글 전체"
        source = "내 로컬 Riot 상세" if player.is_active_player else "저장된 Riot 상세"
        tk.Label(
            card, text=f"{source} · {scope} {stat.games}경기",
            bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7), anchor="w",
        ).pack(fill="x", pady=(5, 0))

        self._render_jungle_advice(card, stat, ally)

    def _render_jungle_advice(
        self,
        card: tk.Widget,
        stat: JungleTendencyStat,
        ally: bool,
    ) -> None:
        advice = jungle_tendency_advice(stat, ally)
        if not advice:
            return
        tk.Label(
            card, text=self._tr("운영 해석"), bg=COLORS["surface"],
            fg=COLORS["blue"], font=("Malgun Gothic", 7, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(8, 2))
        for line in advice:
            tk.Label(
                card, text=f"• {self._tr(line)}", bg=COLORS["surface"],
                fg=COLORS["text"], font=("Malgun Gothic", 7),
                anchor="w", justify="left", wraplength=330,
            ).pack(fill="x", pady=(0, 2))

    def _render_play_plan_card(self, parent: tk.Widget, column: int) -> None:
        border = tk.Frame(parent, bg=COLORS["purple"], padx=1, pady=1)
        border.grid(row=0, column=column, sticky="nsew", padx=3)
        card = tk.Frame(border, bg=COLORS["surface"], padx=12, pady=9)
        card.pack(fill="both", expand=True)
        tk.Label(
            card, text="이번 판 체크 포인트", bg=COLORS["surface"],
            fg=COLORS["purple"], font=("Malgun Gothic", 9, "bold"), anchor="w",
        ).pack(fill="x")
        ready = [
            stat for stat in self.lane_matchups.values()
            if stat.ally_win_rate is not None
        ]
        hints: list[str] = []
        if ready:
            strongest = max(ready, key=lambda stat: stat.ally_win_rate or 0.0)
            weakest = min(ready, key=lambda stat: stat.ally_win_rate or 0.0)
            if (strongest.ally_win_rate or 0.0) >= 51.5:
                hints.append(
                    f"우위 후보 · {ROLE_LABELS.get(strongest.position, strongest.position)} "
                    f"{strongest.ally_champion_name_ko} {(strongest.ally_win_rate or 0):.1f}%"
                )
            if (weakest.ally_win_rate or 100.0) < 48.5:
                hints.append(
                    f"보호 필요 · {ROLE_LABELS.get(weakest.position, weakest.position)} "
                    f"{weakest.ally_champion_name_ko} {(weakest.ally_win_rate or 0):.1f}%"
                )
        enemy_jungler = next(
            (
                player for player in self.live_game.enemies
                if self._comparable_live_position(player.position) == "JUNGLE"
            ),
            None,
        )
        enemy_stat = (
            self.jungle_tendencies.get(enemy_jungler.riot_id)
            if enemy_jungler else None
        )
        if enemy_stat and "갱킹 자주 감" in enemy_stat.labels:
            hints.append("적 정글 초반 개입 표본 높음 · 첫 귀환 전 강가 시야 주의")
        if not hints:
            hints.append("표본이 채워지면 우위 라인과 보호 라인을 자동 표시합니다.")
        for hint in hints[:3]:
            tk.Label(
                card, text=f"• {hint}", bg=COLORS["surface"], fg=COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
                justify="left", wraplength=610,
            ).pack(fill="x", pady=(7, 0))
        tk.Label(
            card,
            text="TOP/MID/BOT 갱 방향은 매치 타임라인 표본이 없어 추정하지 않습니다.",
            bg=COLORS["surface"], fg=COLORS["orange"],
            font=("Malgun Gothic", 7), anchor="w", justify="left",
        ).pack(fill="x", pady=(8, 0))

    def _render_lane_opponent_account(self, parent: tk.Widget) -> None:
        active = next(
            (player for player in self.live_game.players if player.is_active_player),
            None,
        )
        if not active:
            return
        position = self._comparable_live_position(active.position)
        opponent = next(
            (
                player for player in self.live_game.enemies
                if self._comparable_live_position(player.position) == position
            ),
            None,
        )
        if not opponent:
            return
        section = tk.Frame(
            parent, bg=COLORS["panel_2"], padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        section.pack(fill="x", pady=(10, 0))
        heading = tk.Frame(section, bg=COLORS["panel_2"])
        heading.pack(fill="x", pady=(0, 8))
        tk.Label(
            heading,
            text=(
                f"상대 {position_name(position)} 계정 분석 · "
                f"{opponent.riot_id} · {opponent.champion_name_ko}"
            ),
            bg=COLORS["panel_2"], fg=COLORS["red"],
            font=("Malgun Gothic", 11, "bold"), anchor="w",
        ).pack(side="left")
        tk.Label(
            heading,
            text=f"내 픽 {active.champion_name_ko} 기준",
            bg=COLORS["chip"], fg=COLORS["gold"], padx=8, pady=3,
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="right")

        row = tk.Frame(section, bg=COLORS["panel_2"])
        row.pack(fill="x")
        for column in range(4):
            row.grid_columnconfigure(column, weight=1, uniform="opponent_account")
        profile = self.player_profiles.get(opponent.riot_id)
        stat = self._lane_opponent_personal_stat
        matchup = self.lane_matchups.get(position)

        def info_card(column: int, title: str, accent: str) -> tk.Frame:
            outer = tk.Frame(row, bg=COLORS["border"], padx=1, pady=1)
            outer.grid(row=0, column=column, sticky="nsew", padx=3)
            card = tk.Frame(outer, bg=COLORS["surface"], padx=10, pady=8)
            card.pack(fill="both", expand=True)
            tk.Label(
                card, text=title, bg=COLORS["surface"], fg=accent,
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x")
            return card

        account = info_card(0, "계정 최근 폼", COLORS["blue"])
        if profile and profile.status in {"OK", "LOCAL_ONLY", "PARTIAL"}:
            recent_losses = max(profile.recent_games - profile.recent_wins, 0)
            recent_text = (
                f"최근 {profile.recent_games}경기 · "
                f"{profile.recent_wins}승 {recent_losses}패 · "
                f"{(profile.recent_win_rate or 0.0):.0f}%"
                if profile.recent_games else "최근 솔로랭크 표본 없음"
            )
            season_losses = max(profile.season_losses, 0)
            lines: list[str] = []
            if profile.last_game_champion_id:
                last_name = self.registry.by_id.get(
                    profile.last_game_champion_id, (0, profile.last_game_champion_id)
                )[1]
                result = "승" if profile.last_game_won else "패"
                lines.append(
                    f"전판 {result} · {last_name} "
                    f"{profile.last_game_kills}/{profile.last_game_deaths}/"
                    f"{profile.last_game_assists}"
                )
            lines.append(recent_text)
            if profile.recent_games:
                form_detail = f"최근 KDA {(profile.recent_kda or 0.0):.1f}"
                if profile.recent_op_score > 0:
                    form_detail += f" · OP {profile.recent_op_score:.1f}"
                if profile.last_op_score_rank > 0:
                    form_detail += f" · 전판 {profile.last_op_score_rank}등"
                lines.append(form_detail)
            lines.append(
                f"시즌 {profile.season_wins}승 {season_losses}패 · "
                f"{_fmt_rate(profile.season_win_rate)}"
            )
            streak = self._streak_text(profile.overall_streak)
            if streak:
                lines.append(streak)
        else:
            lines = ["OP.GG/로컬 계정 정보 확인 중…"]
        for text_value in (line for line in lines if line):
            tk.Label(
                account, text=text_value, bg=COLORS["surface"], fg=COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x", pady=(5, 0))

        champion = info_card(1, f"{opponent.champion_name_ko} 숙련 표본", COLORS["gold"])
        if profile and profile.status in {"OK", "LOCAL_ONLY", "PARTIAL"}:
            champion_losses = max(profile.champion_games - profile.champion_wins, 0)
            champion_lines = [
                (
                    f"최근 {profile.champion_recent_games}판 · "
                    f"{profile.champion_recent_wins}승 "
                    f"{profile.champion_recent_games - profile.champion_recent_wins}패 · "
                    f"{profile.champion_recent_wins / profile.champion_recent_games * 100:.0f}%"
                    if profile.champion_recent_games else "현 챔프 최근 표본 없음"
                ),
                (
                    f"시즌 {profile.champion_games}판 · {_fmt_rate(profile.champion_win_rate)}"
                    if profile.champion_games else "시즌 상위 챔피언 목록 밖/표본 없음"
                ),
                (
                    f"{profile.champion_wins}승 {champion_losses}패"
                    if profile.champion_games else profile.champion_source_detail
                ),
            ]
            champion_streak = self._streak_text(
                profile.champion_streak,
                f"{self._champion_text(opponent.champion_id, opponent.champion_name_ko)} ",
            )
            if champion_streak:
                champion_lines.append(champion_streak)
            if profile.last_game_champion_id == opponent.champion_id:
                result = "승" if profile.last_game_won else "패"
                champion_lines.append(
                    f"이 챔프 전판 {result} · {profile.last_game_kills}/"
                    f"{profile.last_game_deaths}/{profile.last_game_assists}"
                )
        else:
            champion_lines = ["현재 챔피언 전적 확인 중…"]
        for text_value in (line for line in champion_lines if line):
            tk.Label(
                champion, text=text_value, bg=COLORS["surface"], fg=COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x", pady=(5, 0))

        head_to_head = info_card(2, "내 픽 상대 전적", COLORS["purple"])
        if matchup and matchup.enemy_win_rate is not None:
            tk.Label(
                head_to_head,
                text=(
                    f"OP.GG 게임 · {opponent.champion_name_ko} "
                    f"{matchup.enemy_win_rate:.1f}%"
                ),
                bg=COLORS["surface"], fg=COLORS["purple"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x", pady=(5, 0))
        if matchup and matchup.enemy_laning_win_rate is not None:
            lane_value = f"라인전 · {matchup.enemy_laning_win_rate:.1f}%"
        else:
            lane_value = "라인전 · 통계 소스 미제공"
        tk.Label(
            head_to_head, text=lane_value, bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Malgun Gothic", 8), anchor="w",
        ).pack(fill="x", pady=(5, 0))
        if self._lane_opponent_analysis_loading:
            local_text = "계정 맞상대 로컬 표본 계산 중…"
        elif stat and stat.matchup_games:
            local_text = (
                f"이 계정: {opponent.champion_name_ko}로 {active.champion_name_ko} 상대 "
                f"{stat.matchup_games}판 · {_fmt_rate(stat.matchup_win_rate)}"
            )
        else:
            local_text = "이 계정의 해당 맞상대 로컬 표본 없음"
        tk.Label(
            head_to_head, text=local_text, bg=COLORS["surface"],
            fg=COLORS["orange"] if not (stat and stat.matchup_games) else COLORS["green"],
            font=("Malgun Gothic", 8, "bold"), anchor="w",
            justify="left", wraplength=340,
        ).pack(fill="x", pady=(6, 0))

        response = info_card(3, "상대 대응 메모", COLORS["green"])
        archetype = support_archetype(opponent.champion_id) if position == "SUPPORT" else "OTHER"
        response_hints = {
            "ENGAGE": ["2레벨 선진입과 부시 각을 먼저 경계", "핵심 진입기가 빠지면 견제·푸시 전환"],
            "POKE": ["첫 귀환 전 체력 손실과 라인 푸시 관리", "핵심 견제 스킬 후 전진해 교환"],
            "UTILITY": ["보호 스킬 쿨타임 뒤 교환 집중", "한 번에 잡기보다 마나·체력 격차 누적"],
            "OTHER": ["최근 폼과 상성 수치를 함께 보고 교전 강도 조절", "표본이 적으면 챔피언 기본 상성을 우선"],
        }[archetype]
        if profile and profile.overall_streak <= -3:
            response_hints.insert(0, "상대 최근 연패 · 무리한 복구 플레이 가능성 주의")
        elif profile and profile.overall_streak >= 3:
            response_hints.insert(0, "상대 최근 연승 · 초반 숙련도 과소평가 금지")
        for hint in response_hints[:3]:
            tk.Label(
                response, text=f"• {hint}", bg=COLORS["surface"], fg=COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
                justify="left", wraplength=340,
            ).pack(fill="x", pady=(5, 0))

        behavior_outer = tk.Frame(
            section, bg=COLORS["blue"], padx=1, pady=1,
        )
        behavior_outer.pack(fill="x", pady=(10, 0))
        behavior_panel = tk.Frame(
            behavior_outer, bg=COLORS["surface"], padx=11, pady=9,
        )
        behavior_panel.pack(fill="x")
        behavior_top = tk.Frame(behavior_panel, bg=COLORS["surface"])
        behavior_top.pack(fill="x")
        tk.Label(
            behavior_top, text="최근 행동 패턴 · 분석가 요약",
            bg=COLORS["surface"], fg=COLORS["blue"],
            font=("Malgun Gothic", 9, "bold"), anchor="w",
        ).pack(side="left")
        behavior = self._lane_opponent_behavior
        if behavior and behavior.games:
            confidence = (
                "표본 충분" if behavior.games >= 10
                else "표본 보통" if behavior.games >= 5
                else "표본 적음"
            )
            scope = "현 챔프" if behavior.champion_specific else "현 포지션"
            tk.Label(
                behavior_top,
                text=f"로컬 솔로랭크 · {scope} {behavior.games}경기 · {confidence}",
                bg=COLORS["chip"],
                fg=COLORS["green"] if behavior.games >= 5 else COLORS["orange"],
                padx=8, pady=3, font=("Malgun Gothic", 7, "bold"),
            ).pack(side="right")
            badge_row = tk.Frame(behavior_panel, bg=COLORS["surface"])
            badge_row.pack(fill="x", pady=(7, 6))
            for label in behavior.labels[:5]:
                badge_color = (
                    COLORS["red"] if label in {
                        "데스 많음", "초반 라인 약세", "합류 낮음",
                        "시야 부족", "제어 와드 부족",
                    }
                    else COLORS["green"] if label in {
                        "초반 라인 강함", "시야 좋음", "생존 안정",
                        "합류 잦음", "제어 와드 적극",
                        "군중 통제 강함", "좋은 탱킹", "회복·보호 강함",
                        "공격적 딜링", "오브젝트 기여", "철거 기여",
                    }
                    else COLORS["gold"]
                )
                tk.Label(
                    badge_row, text=self._tr(label), bg=COLORS["chip"], fg=badge_color,
                    padx=7, pady=2, font=("Malgun Gothic", 7, "bold"),
                ).pack(side="left", padx=(0, 5))

            metric_row = tk.Frame(behavior_panel, bg=COLORS["surface"])
            metric_row.pack(fill="x")
            metrics: list[tuple[str, str, str]] = [
                (
                    "선취점 관여",
                    f"{(behavior.first_blood_rate or 0.0):.0f}%",
                    f"직접 킬 {behavior.first_blood_kills} · 어시 {behavior.first_blood_assists}",
                ),
                (
                    "초반 라인 우위",
                    (
                        f"{behavior.early_advantage_rate:.0f}%"
                        if behavior.early_advantage_rate is not None else "미제공"
                    ),
                    "골드·경험치 우위 경기",
                ),
                (
                    "초반 관여",
                    (
                        f"{behavior.early_takedowns:.1f}회"
                        if behavior.early_takedowns is not None else "미제공"
                    ),
                    "Riot 초반 처치 관여",
                ),
                (
                    "킬 관여율",
                    (
                        f"{behavior.kill_participation:.0f}%"
                        if behavior.kill_participation is not None else "미제공"
                    ),
                    "한타·합류 성향",
                ),
                (
                    "평균 데스",
                    f"{(behavior.average_deaths or 0.0):.1f}",
                    "진입 위험도 참고",
                ),
                (
                    "시야 투자",
                    (
                        f"분당 {behavior.vision_per_minute:.2f}"
                        if behavior.vision_per_minute is not None else "미제공"
                    ),
                    (
                        f"제어 와드 {behavior.control_wards:.1f}개"
                        if behavior.control_wards is not None else "제어 와드 미제공"
                    ),
                ),
            ]
            for column, (title, value, detail) in enumerate(metrics):
                metric_row.grid_columnconfigure(column, weight=1, uniform="behavior_metrics")
                metric = tk.Frame(metric_row, bg=COLORS["panel_2"], padx=8, pady=6)
                metric.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 3, 3))
                tk.Label(
                    metric, text=title, bg=COLORS["panel_2"], fg=COLORS["muted"],
                    font=("Malgun Gothic", 7, "bold"), anchor="w",
                ).pack(fill="x")
                tk.Label(
                    metric, text=value, bg=COLORS["panel_2"], fg=COLORS["text"],
                    font=("Malgun Gothic", 10, "bold"), anchor="w",
                ).pack(fill="x", pady=(2, 0))
                tk.Label(
                    metric, text=detail, bg=COLORS["panel_2"], fg=COLORS["muted"],
                    font=("Malgun Gothic", 6), anchor="w", wraplength=180,
                ).pack(fill="x", pady=(2, 0))

            signal_row = tk.Frame(behavior_panel, bg=COLORS["surface"])
            signal_row.pack(fill="x", pady=(9, 0))
            signal_row.grid_columnconfigure(0, weight=1, uniform="opponent_signals")
            signal_row.grid_columnconfigure(1, weight=1, uniform="opponent_signals")
            signal_sets = (
                (
                    "경계할 강점", COLORS["red"],
                    behavior_strength_signals(behavior, position),
                ),
                (
                    "공략할 약점", COLORS["green"],
                    behavior_weakness_signals(behavior, position),
                ),
            )
            for column, (title, accent, signals) in enumerate(signal_sets):
                signal_card = tk.Frame(
                    signal_row, bg=COLORS["panel_2"], padx=10, pady=7,
                )
                signal_card.grid(
                    row=0, column=column, sticky="nsew",
                    padx=(0, 3) if column == 0 else (3, 0),
                )
                tk.Label(
                    signal_card, text=title, bg=COLORS["panel_2"], fg=accent,
                    font=("Malgun Gothic", 8, "bold"), anchor="w",
                ).pack(fill="x")
                for signal in signals[:4]:
                    tk.Label(
                        signal_card, text=f"• {signal}", bg=COLORS["panel_2"],
                        fg=COLORS["text"], font=("Malgun Gothic", 8),
                        anchor="w", justify="left", wraplength=700,
                    ).pack(fill="x", pady=(4, 0))
        else:
            waiting = (
                "최근 행동 원본을 백그라운드에서 분석 중…"
                if self._lane_opponent_analysis_loading
                else "저장된 Riot 솔로랭크 원본이 없어 행동 성향을 추정하지 않습니다."
            )
            tk.Label(
                behavior_panel, text=waiting, bg=COLORS["surface"],
                fg=COLORS["muted"], font=("Malgun Gothic", 8), anchor="w",
            ).pack(fill="x", pady=(7, 0))

    def _render_my_account_coaching(self, parent: tk.Widget) -> None:
        active = next(
            (player for player in self.live_game.players if player.is_active_player),
            None,
        )
        if not active:
            return
        position = self._comparable_live_position(active.position)
        opponent = next(
            (
                player for player in self.live_game.enemies
                if self._comparable_live_position(player.position) == position
            ),
            None,
        )
        ally_adc = next(
            (
                player for player in self.live_game.allies
                if self._comparable_live_position(player.position) == "BOTTOM"
            ),
            None,
        ) if position == "SUPPORT" else None
        profile = self.player_profiles.get(active.riot_id)
        stat = self._my_personal_stat
        behavior = self._my_behavior
        enemy_behavior = self._lane_opponent_behavior
        matchup = self.lane_matchups.get(position)

        section = tk.Frame(
            parent, bg=COLORS["panel_2"], padx=12, pady=11,
            highlightthickness=1, highlightbackground=COLORS["green"],
        )
        section.pack(fill="x", pady=(10, 0))
        heading = tk.Frame(section, bg=COLORS["panel_2"])
        heading.pack(fill="x", pady=(0, 8))
        tk.Label(
            heading,
            text=(
                f"내 계정 분석 · {active.riot_id} · {active.champion_name_ko}"
                + (f" vs {opponent.champion_name_ko}" if opponent else "")
            ),
            bg=COLORS["panel_2"], fg=COLORS["green"],
            font=("Malgun Gothic", 11, "bold"), anchor="w",
        ).pack(side="left")
        tk.Label(
            heading, text="솔로랭크 근거 · 이번 판 코칭",
            bg=COLORS["chip"], fg=COLORS["gold"], padx=8, pady=3,
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="right")

        overview = tk.Frame(section, bg=COLORS["panel_2"])
        overview.pack(fill="x")
        for column in range(4):
            overview.grid_columnconfigure(column, weight=1, uniform="my_account")

        def account_card(column: int, title: str, accent: str) -> tk.Frame:
            outer = tk.Frame(overview, bg=COLORS["border"], padx=1, pady=1)
            outer.grid(row=0, column=column, sticky="nsew", padx=3)
            card = tk.Frame(outer, bg=COLORS["surface"], padx=10, pady=8)
            card.pack(fill="both", expand=True)
            tk.Label(
                card, text=title, bg=COLORS["surface"], fg=accent,
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x")
            return card

        recent = account_card(0, "내 최근 폼", COLORS["blue"])
        recent_lines: list[str] = []
        if profile and profile.status in {"OK", "LOCAL_ONLY", "PARTIAL"}:
            if profile.last_game_champion_id:
                last_name = self.registry.by_id.get(
                    profile.last_game_champion_id, (0, profile.last_game_champion_id)
                )[1]
                result = "승" if profile.last_game_won else "패"
                recent_lines.append(
                    f"전판 {result} · {last_name} "
                    f"{profile.last_game_kills}/{profile.last_game_deaths}/"
                    f"{profile.last_game_assists}"
                )
            if profile.recent_games:
                losses = profile.recent_games - profile.recent_wins
                recent_lines.append(
                    f"최근 {profile.recent_games}판 · {profile.recent_wins}승 "
                    f"{losses}패 · {(profile.recent_win_rate or 0.0):.0f}%"
                )
                recent_lines.append(f"최근 KDA {(profile.recent_kda or 0.0):.1f}")
            streak = self._streak_text(profile.overall_streak)
            if streak:
                recent_lines.append(streak)
            if profile.season_wins + profile.season_losses:
                recent_lines.append(
                    f"시즌 {profile.season_wins}승 {profile.season_losses}패 · "
                    f"{_fmt_rate(profile.season_win_rate)}"
                )
        if not recent_lines:
            recent_lines = ["내 최근 솔로랭크 정보 확인 중…"]
        for value in recent_lines[:4]:
            tk.Label(
                recent, text=value, bg=COLORS["surface"], fg=COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x", pady=(5, 0))

        champion = account_card(1, f"내 {active.champion_name_ko} 기록", COLORS["gold"])
        if self._my_account_analysis_loading:
            champion_lines = ["로컬 챔피언 표본 계산 중…"]
        elif stat and stat.games:
            champion_lines = [
                f"{stat.games}판 · {stat.wins}승 {stat.losses}패 · {_fmt_rate(stat.win_rate)}",
                f"KDA {(stat.kda or 0.0):.2f} · 평균 시야 {(stat.vision_score or 0.0):.1f}",
                (
                    f"최근 행동 {behavior.games}판 · "
                    f"{'현 챔프' if behavior and behavior.champion_specific else position_name(position)}"
                    if behavior and behavior.games else "최근 행동 표본 없음"
                ),
            ]
        else:
            champion_lines = ["해당 포지션·챔피언 로컬 표본 없음"]
        for value in champion_lines:
            tk.Label(
                champion, text=value, bg=COLORS["surface"], fg=COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x", pady=(5, 0))

        context_card = account_card(2, "맞상대 · 조합 기록", COLORS["purple"])
        context_lines: list[tuple[str, str]] = []
        if opponent and stat and stat.matchup_games:
            context_lines.append((
                f"vs {opponent.champion_name_ko} · {stat.matchup_games}판 · "
                f"{_fmt_rate(stat.matchup_win_rate)} · {stat.matchup_confidence}",
                COLORS["green"] if (stat.matchup_win_rate or 0.0) >= 50.0 else COLORS["orange"],
            ))
        elif opponent:
            context_lines.append((f"vs {opponent.champion_name_ko} · 내 로컬 표본 없음", COLORS["muted"]))
        if matchup and matchup.ally_win_rate is not None:
            context_lines.append((
                f"OP.GG 전체 · {matchup.ally_win_rate:.1f}% · "
                f"{lane_matchup_label(matchup.ally_win_rate)}",
                COLORS["purple"],
            ))
        if ally_adc and stat and stat.ally_adc_games:
            context_lines.append((
                f"{ally_adc.champion_name_ko} 조합 · {stat.ally_adc_games}판 · "
                f"{_fmt_rate(stat.ally_adc_win_rate)}",
                COLORS["blue"],
            ))
        elif ally_adc:
            context_lines.append((f"{ally_adc.champion_name_ko} 조합 · 내 표본 없음", COLORS["muted"]))
        if not context_lines:
            context_lines = [("현재 조합 표본 대기 중", COLORS["muted"])]
        for value, color in context_lines[:4]:
            tk.Label(
                context_card, text=value, bg=COLORS["surface"], fg=color,
                font=("Malgun Gothic", 8, "bold"), anchor="w",
                justify="left", wraplength=340,
            ).pack(fill="x", pady=(5, 0))

        diagnosis = account_card(3, "강점 · 고칠 약점", COLORS["green"])
        strengths = behavior_strength_signals(behavior, position)
        weaknesses = behavior_weakness_signals(behavior, position)
        for prefix, value, color in (
            ("강점", strengths[0], COLORS["green"]),
            ("강점", strengths[1], COLORS["green"]) if len(strengths) > 1 else ("", "", COLORS["green"]),
            ("약점", weaknesses[0], COLORS["red"]),
            ("약점", weaknesses[1], COLORS["red"]) if len(weaknesses) > 1 else ("", "", COLORS["red"]),
        ):
            if not value:
                continue
            tk.Label(
                diagnosis, text=f"{prefix} · {value}", bg=COLORS["surface"], fg=color,
                font=("Malgun Gothic", 8, "bold"), anchor="w",
                justify="left", wraplength=340,
            ).pack(fill="x", pady=(5, 0))

        comparison_outer = tk.Frame(section, bg=COLORS["green"], padx=1, pady=1)
        comparison_outer.pack(fill="x", pady=(10, 0))
        comparison = tk.Frame(comparison_outer, bg=COLORS["surface"], padx=11, pady=9)
        comparison.pack(fill="x")
        comparison_top = tk.Frame(comparison, bg=COLORS["surface"])
        comparison_top.pack(fill="x", pady=(0, 7))
        tk.Label(
            comparison_top, text="나 vs 맞라인 상대 · 최근 행동 비교",
            bg=COLORS["surface"], fg=COLORS["green"],
            font=("Malgun Gothic", 9, "bold"), anchor="w",
        ).pack(side="left")
        tk.Label(
            comparison_top,
            text=(
                f"내 {behavior.games if behavior else 0}경기 / "
                f"상대 {enemy_behavior.games if enemy_behavior else 0}경기"
            ),
            bg=COLORS["chip"], fg=COLORS["muted"], padx=8, pady=3,
            font=("Malgun Gothic", 7, "bold"),
        ).pack(side="right")
        metric_row = tk.Frame(comparison, bg=COLORS["surface"])
        metric_row.pack(fill="x")

        def metric_value(source: PlayerBehaviorStat | None, field: str, suffix: str) -> str:
            value = getattr(source, field, None) if source and source.games else None
            if value is None:
                return "--"
            return f"{float(value):.2f}{suffix}" if field == "vision_per_minute" else f"{float(value):.1f}{suffix}"

        comparisons = [
            ("선취점 관여", "first_blood_rate", "%"),
            ("초반 우위", "early_advantage_rate", "%"),
            ("킬 관여", "kill_participation", "%"),
            ("평균 데스", "average_deaths", ""),
            ("분당 시야", "vision_per_minute", ""),
        ]
        for column, (title, field, suffix) in enumerate(comparisons):
            metric_row.grid_columnconfigure(column, weight=1, uniform="my_comparison")
            metric = tk.Frame(metric_row, bg=COLORS["panel_2"], padx=8, pady=6)
            metric.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 3, 3))
            tk.Label(
                metric, text=title, bg=COLORS["panel_2"], fg=COLORS["muted"],
                font=("Malgun Gothic", 7, "bold"), anchor="w",
            ).pack(fill="x")
            tk.Label(
                metric,
                text=f"나 {metric_value(behavior, field, suffix)}",
                bg=COLORS["panel_2"], fg=COLORS["green"],
                font=("Malgun Gothic", 9, "bold"), anchor="w",
            ).pack(fill="x", pady=(2, 0))
            tk.Label(
                metric,
                text=f"상대 {metric_value(enemy_behavior, field, suffix)}",
                bg=COLORS["panel_2"], fg=COLORS["red"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x", pady=(1, 0))

        plan_outer = tk.Frame(section, bg=COLORS["gold"], padx=1, pady=1)
        plan_outer.pack(fill="x", pady=(10, 0))
        plan_panel = tk.Frame(plan_outer, bg=COLORS["surface"], padx=11, pady=9)
        plan_panel.pack(fill="x")
        tk.Label(
            plan_panel, text="이번 판 맞춤 실행 계획",
            bg=COLORS["surface"], fg=COLORS["gold"],
            font=("Malgun Gothic", 9, "bold"), anchor="w",
        ).pack(fill="x", pady=(0, 7))
        plans = tk.Frame(plan_panel, bg=COLORS["surface"])
        plans.pack(fill="x")
        for column in range(3):
            plans.grid_columnconfigure(column, weight=1, uniform="coaching_plans")

        my_archetype = support_archetype(active.champion_id) if position == "SUPPORT" else "OTHER"
        enemy_archetype = support_archetype(opponent.champion_id) if position == "SUPPORT" and opponent else "OTHER"
        lane_plan = {
            "ENGAGE": "핵심 진입기와 2레벨 타이밍을 핑으로 맞추고, 실패하면 즉시 이탈",
            "POKE": "웨이브를 망치지 않는 각에서 견제하고 핵심 스킬 적중 뒤만 전진",
            "UTILITY": "원딜 체력·핵심 보호 스킬을 기준으로 짧은 교환을 반복",
            "OTHER": "상성 수치보다 첫 두 웨이브 결과를 보고 교전 강도를 조절",
        }[my_archetype]
        if matchup and matchup.ally_win_rate is not None and matchup.ally_win_rate < 48.5:
            lane_plan += " · 불리 표본이므로 무리한 선공보다 정글 동선 확인 우선"
        elif enemy_archetype == "ENGAGE":
            lane_plan += " · 상대 진입기가 빠진 직후가 가장 안전한 반격 창"

        vision_plan = "첫 귀환 제어 와드, 3분 전후 강가·삼거리 시야를 먼저 예약"
        if behavior and behavior.vision_per_minute is not None and behavior.vision_per_minute < 1.3:
            vision_plan = "내 시야 약점 우선 교정 · 귀환마다 제어 와드 1개, 오브젝트 60초 전 설치"
        enemy_jungler = next(
            (
                player for player in self.live_game.enemies
                if self._comparable_live_position(player.position) == "JUNGLE"
            ),
            None,
        )
        enemy_jungle_stat = self.jungle_tendencies.get(enemy_jungler.riot_id) if enemy_jungler else None
        if enemy_jungle_stat and "갱킹 자주 감" in enemy_jungle_stat.labels:
            vision_plan += " · 적 정글 초반 개입 표본 높아 첫 귀환 전 부시 체크"

        fight_plan = {
            "ENGAGE": "우리 딜러가 닿는 거리에서만 시작하고, 두 번째 진입 수단은 이탈용으로 보존",
            "POKE": "오브젝트 전 체력을 먼저 깎고, 시야 없는 곳으로 단독 진입하지 않기",
            "UTILITY": "가장 잘 큰 아군을 지정해 핵심 보호기와 탈진을 겹치지 않게 사용",
            "OTHER": "교전 전 아군 핵심 딜러 위치와 적 주요 스킬 하나를 확인한 뒤 진입",
        }[my_archetype]
        if behavior and behavior.average_deaths is not None and behavior.average_deaths >= 6.0:
            fight_plan += " · 내 평균 데스가 높아 한타 첫 진입 후 재진입 금지"
        elif behavior and behavior.kill_participation is not None and behavior.kill_participation <= 50.0:
            fight_plan += " · 내 합류율이 낮아 사이드보다 다음 오브젝트 쪽에 30초 먼저 이동"

        for column, (title, text_value, accent) in enumerate((
            ("라인전", lane_plan, COLORS["blue"]),
            ("시야 · 오브젝트", vision_plan, COLORS["green"]),
            ("한타 · 교전", fight_plan, COLORS["purple"]),
        )):
            card = tk.Frame(plans, bg=COLORS["panel_2"], padx=10, pady=8)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 3, 3))
            tk.Label(
                card, text=title, bg=COLORS["panel_2"], fg=accent,
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(fill="x")
            tk.Label(
                card, text=text_value, bg=COLORS["panel_2"], fg=COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), anchor="w",
                justify="left", wraplength=480,
            ).pack(fill="x", pady=(5, 0))
        tk.Label(
            plan_panel,
            text="표본이 적으면 약점을 확정하지 않습니다. 수치는 저장된 솔로랭크 경기만 사용합니다.",
            bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7), anchor="w",
        ).pack(fill="x", pady=(8, 0))

    def _play_summary_state_signature(self) -> str:
        """Return only inputs used by the top metrics and win prediction."""
        players = tuple(
            (
                player.riot_id, player.champion_id, player.team,
                player.is_active_player,
                (
                    profile.status,
                    profile.tier, profile.rank, profile.league_points,
                    profile.season_wins, profile.season_losses,
                    profile.recent_games, profile.recent_wins,
                    profile.recent_kills, profile.recent_deaths,
                    profile.recent_assists, profile.overall_streak,
                    profile.champion_games, profile.champion_wins,
                    profile.last_game_champion_id,
                    profile.last_game_kills, profile.last_game_deaths,
                    profile.last_game_assists, profile.last_game_won,
                    profile.last_op_score_rank,
                ) if (profile := self.player_profiles.get(player.riot_id)) else None,
            )
            for player in self.live_game.players
        )
        duo_pairs = tuple(
            (
                riot_id,
                tuple(sorted((other, level) for other, level, _evidence in values)),
            )
            for riot_id, values in sorted(self.duo_pairs.items())
        )
        matchups = tuple(
            (position, stat.ally_win_rate)
            for position, stat in sorted(getattr(self, "lane_matchups", {}).items())
        )
        refreshing = self._live_signature in getattr(
            self, "_lane_matchup_refreshing", set()
        )
        return repr((
            self.live_game.active_team, self.live_game.active_riot_id,
            players, duo_pairs, matchups, refreshing,
            getattr(self, "_profiles_loading", False),
            getattr(self, "_opgg_profiles_loading", False),
            getattr(self, "_duo_checking", False),
        ))

    def _render_play_prediction(self) -> None:
        """Update the compact prediction bar without rebuilding lower insights."""
        if not hasattr(self, "play_prediction_frame"):
            return
        prediction = self._live_prediction
        signature = game_prediction_display_signature(prediction)
        if signature == self._play_prediction_signature:
            return
        self._play_prediction_signature = signature
        if prediction is None:
            if self.play_prediction_frame.winfo_manager():
                self.play_prediction_frame.pack_forget()
            return

        prediction_color = (
            COLORS["orange"] if prediction.evidence_score < 0.15
            else COLORS["green"] if prediction.predicted_win else COLORS["red"]
        )
        result_text = (
            "표본 수집 중" if prediction.evidence_score < 0.15
            else "승리 예상" if prediction.predicted_win else "패배 예상"
        )
        self.play_prediction_frame.configure(
            highlightbackground=prediction_color,
        )
        self.play_prediction_value.configure(
            text=f"예상 승률 {prediction.win_probability:.1f}% · {result_text}",
            fg=prediction_color,
        )
        self.play_prediction_detail.configure(
            text=(
                f"신뢰도 {prediction.confidence} · "
                + "  |  ".join(prediction.evidence[:2])
            ),
        )
        if not self.play_prediction_frame.winfo_manager():
            self.play_prediction_frame.pack(
                fill="x", pady=(0, 10), before=self.play_insight_body,
            )

    def _render_play_summary(self) -> None:
        signature = self._play_summary_state_signature()
        if signature == self._play_summary_signature:
            return
        self._play_summary_signature = signature

        def team_rates(players: list[LivePlayer]) -> list[float]:
            rates: list[float] = []
            for player in players:
                profile = self.player_profiles.get(player.riot_id)
                if (
                    profile and profile.status in {"OK", "LOCAL_ONLY", "PARTIAL"}
                    and profile.season_win_rate is not None
                ):
                    rates.append(profile.season_win_rate)
            return rates

        ally_rates = team_rates(self.live_game.allies)
        enemy_rates = team_rates(self.live_game.enemies)
        ally_average = sum(ally_rates) / len(ally_rates) if ally_rates else None
        enemy_average = sum(enemy_rates) / len(enemy_rates) if enemy_rates else None
        difference = (
            ally_average - enemy_average
            if ally_average is not None and enemy_average is not None else None
        )
        ally_detail = self._text("play.rank_sample", count=len(ally_rates))
        enemy_detail = self._text("play.rank_sample", count=len(enemy_rates))
        if difference is not None:
            ally_detail += self._text("play.compare_enemy", difference=difference)
            enemy_detail += self._text("play.compare_ally", difference=-difference)
        self.play_metrics["ally"][0].configure(text=_fmt_rate(ally_average))
        self.play_metrics["ally"][1].configure(text=ally_detail)
        self.play_metrics["enemy"][0].configure(text=_fmt_rate(enemy_average))
        self.play_metrics["enemy"][1].configure(text=enemy_detail)
        loaded = sum(
            1 for profile in self.player_profiles.values()
            if profile.status in {"OK", "LOCAL_ONLY", "PARTIAL"}
        )
        total = len(self.live_game.players)
        self.play_metrics["cache"][0].configure(
            text=self._text("play.checked_players", loaded=loaded, total=total or 10)
        )
        self.play_metrics["cache"][1].configure(
            text=(
                self._text("play.data_sources") if loaded
                else self._text("play.auto_check")
            )
        )
        duo_groups = {
            visual[0]: visual[3]
            for visual in duo_group_visuals(
                self.live_game.players, self.duo_pairs,
                active_team=self.live_game.active_team,
            ).values()
        }
        strongest = (
            max(
                duo_groups.values(),
                key=lambda level: DUO_LEVEL_PRIORITY.get(level, 0),
            )
            if duo_groups else self._text("common.none")
        )
        self.play_metrics["duo"][0].configure(
            text=self._text("play.duo_pairs", count=len(duo_groups))
        )
        self.play_metrics["duo"][1].configure(
            text=self._text("play.strongest_signal", signal=self._tr(strongest))
        )
        matchups = list(getattr(self, "lane_matchups", {}).values())
        ready_matchups = sum(stat.ally_win_rate is not None for stat in matchups)
        refreshing = self._live_signature in getattr(
            self, "_lane_matchup_refreshing", set()
        )
        self.play_metrics["matchup"][0].configure(
            text=self._text(
                "play.matchup_lines", ready=ready_matchups,
                total=len(matchups) or 5,
            )
        )
        self.play_metrics["matchup"][1].configure(
            text=(
                self._text("play.matchup_refreshing") if refreshing
                else self._text("play.matchup_cache")
            )
        )
        self._update_live_prediction()

    def _prediction_inputs_settled(self) -> bool:
        """Return whether every currently running prediction source has settled."""
        signature = getattr(self, "_live_signature", "")
        return not (
            getattr(self, "_profiles_loading", False)
            or getattr(self, "_opgg_profiles_loading", False)
            or getattr(self, "_duo_checking", False)
            or signature in getattr(self, "_lane_matchup_refreshing", set())
        )

    def _cancel_prediction_settle_wakeup(self) -> None:
        callback_id = getattr(self, "_prediction_settle_after_id", None)
        self._prediction_settle_after_id = None
        if not callback_id:
            return
        try:
            self.root.after_cancel(callback_id)
        except (AttributeError, tk.TclError):
            pass

    def _schedule_prediction_settle_wakeup(self, prediction_key: str) -> None:
        if getattr(self, "_prediction_settle_after_id", None):
            return
        started_at = float(
            getattr(self, "_prediction_settle_started_at", 0.0) or time.monotonic()
        )
        elapsed = max(0.0, time.monotonic() - started_at)
        delay_ms = max(
            1,
            int(round(
                max(0.0, PREDICTION_SETTLE_TIMEOUT_SECONDS - elapsed) * 1000.0
            )),
        )

        def wake() -> None:
            self._prediction_settle_after_id = None
            if prediction_key != getattr(self, "_prediction_baseline_key", ""):
                return
            self._update_live_prediction()
            self._render_play_prediction()

        self._prediction_settle_after_id = self.root.after(delay_ms, wake)

    def _update_live_prediction(self) -> None:
        value_label, detail_label = self.play_metrics["prediction"]
        if getattr(self, "_showing_previous_play", False):
            prediction = self._live_prediction
            if prediction is None:
                value_label.configure(text="--", fg=COLORS["gold"])
                detail_label.configure(
                    text=self._text("play.previous_prediction_unavailable")
                )
                return
            if prediction.evidence_score < 0.15:
                result_text = "표본 수집 중"
                result_color = COLORS["orange"]
            else:
                result_text = "승리 예상" if prediction.predicted_win else "패배 예상"
                result_color = COLORS["green"] if prediction.predicted_win else COLORS["red"]
            value_label.configure(
                text=f"{prediction.win_probability:.1f}% · {self._tr(result_text)}",
                fg=result_color,
            )
            detail_label.configure(text=self._text("play.previous_prediction"))
            return
        if not self.live_game.players:
            self._live_prediction = None
            value_label.configure(text="--", fg=COLORS["gold"])
            detail_label.configure(text=self._text("play.prediction_waiting"))
            return
        candidate = estimate_live_game_prediction(
            self.live_game,
            self.player_profiles,
            getattr(self, "lane_matchups", {}),
            self.duo_pairs,
        )
        if candidate.prediction_key != getattr(
            self, "_prediction_baseline_key", "",
        ):
            self._cancel_prediction_settle_wakeup()
            pending_save = getattr(self, "_prediction_save_after_id", None)
            if pending_save:
                try:
                    self.root.after_cancel(pending_save)
                except (AttributeError, tk.TclError):
                    pass
                self._prediction_save_after_id = None
                self._prediction_save_queued = None
            self._prediction_baseline_key = candidate.prediction_key
            self._prediction_settle_started_at = time.monotonic()
            self._prediction_baseline = (
                self.storage.load_game_prediction_by_key(
                    candidate.prediction_key
                )
                if (
                    not self.demo
                    and hasattr(self.storage, "load_game_prediction_by_key")
                ) else None
            )
        # A stored baseline is immutable evidence for the later accuracy
        # report.  It must never freeze the live UI while fresher profile and
        # matchup inputs continue to arrive.
        prediction = candidate
        self._live_prediction = prediction
        if prediction.evidence_score < 0.15:
            result_text = "표본 수집 중"
            result_color = COLORS["orange"]
        else:
            result_text = "승리 예상" if prediction.predicted_win else "패배 예상"
            result_color = COLORS["green"] if prediction.predicted_win else COLORS["red"]
        value_label.configure(
            text=f"{prediction.win_probability:.1f}% · {result_text}",
            fg=result_color,
        )
        detail_label.configure(
            text=f"신뢰도 {prediction.confidence} · 시작 전 지표"
        )
        save_signature = repr((
            prediction.prediction_key, prediction.win_probability,
            prediction.confidence, prediction.evidence,
            prediction.evidence_score,
        ))
        if (
            not self.demo and prediction.prediction_key
            and prediction.active_riot_id and len(self.live_game.players) == 10
            and prediction.evidence_score >= 0.15
            and self._prediction_baseline is None
            and not getattr(self, "_prediction_save_running", False)
            and save_signature != self._prediction_saved_signature
        ):
            elapsed = max(
                0.0,
                time.monotonic() - float(
                    getattr(self, "_prediction_settle_started_at", 0.0)
                    or time.monotonic()
                ),
            )
            if (
                self._prediction_inputs_settled()
                or elapsed >= PREDICTION_SETTLE_TIMEOUT_SECONDS
            ):
                self._cancel_prediction_settle_wakeup()
                self._schedule_game_prediction_save(prediction, save_signature)
            else:
                self._schedule_prediction_settle_wakeup(
                    prediction.prediction_key
                )

    def _schedule_game_prediction_save(
        self, prediction: GamePrediction, save_signature: str,
    ) -> None:
        """Persist only the settled prediction, outside Tk's event thread."""
        self._prediction_save_queued = (prediction, save_signature)
        pending_after = getattr(self, "_prediction_save_after_id", None)
        if pending_after:
            try:
                self.root.after_cancel(pending_after)
            except tk.TclError:
                pass

        def persist() -> None:
            self._prediction_save_after_id = None
            self._start_game_prediction_save()

        # Profile and lane results arrive in a burst.  Waiting briefly avoids
        # a SQLite write for every intermediate probability while the visible
        # label still updates immediately.
        self._prediction_save_after_id = self.root.after(
            PREDICTION_SAVE_DEBOUNCE_MS, persist
        )

    def _start_game_prediction_save(self) -> None:
        """Run prediction writes one at a time and keep only the newest queued value."""
        if getattr(self, "_prediction_save_running", False):
            return
        queued = getattr(self, "_prediction_save_queued", None)
        if queued is None:
            return
        prediction, save_signature = queued
        self._prediction_save_queued = None
        if save_signature == self._prediction_saved_signature:
            return
        self._prediction_save_running = True
        self._prediction_save_pending_signature = save_signature

        def finish() -> None:
            self._prediction_save_running = False
            if self._prediction_save_pending_signature == save_signature:
                self._prediction_save_pending_signature = ""
            # A newer probability may have settled while this write was in
            # flight.  Start it only after this one completes so SQLite cannot
            # be overwritten out of order.
            self._start_game_prediction_save()

        def success(result: object) -> None:
            baseline = result if isinstance(result, GamePrediction) else prediction
            if baseline.prediction_key == getattr(
                self, "_prediction_baseline_key", "",
            ):
                self._prediction_baseline = baseline
            self._prediction_saved_signature = save_signature
            finish()

        def error(_exc: Exception) -> None:
            if self._live_prediction is prediction:
                self.play_metrics["prediction"][1].configure(
                    text="신뢰도 계산 완료 · 로컬 기록 저장 실패"
                )
            finish()

        self._background(
            lambda: self.storage.save_game_prediction(prediction),
            success,
            error,
        )

    @staticmethod
    def _configure_player_card_icon(
        label: tk.Label, image: tk.PhotoImage,
    ) -> None:
        """Patch one image label without rebuilding the player's whole card."""
        label.configure(image=image, text="", width=0, height=0)
        # Keep an explicit Tk reference even though the shared cache also owns
        # it; this makes the label safe if the cache implementation changes.
        label.image = image  # type: ignore[attr-defined]

    def _update_player_card_icon(
        self, label: tk.Label, champion_id: str, size: int,
    ) -> None:
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return
        image = self.icon_cache.get(champion_id, size)
        if not image:
            return
        try:
            self._configure_player_card_icon(label, image)
        except tk.TclError:
            return

    def _render_player_card(
        self, parent: tk.Widget, player: LivePlayer, ally: bool, slot: int
    ) -> None:
        team_color = COLORS["blue"] if ally else COLORS["red"]
        border_color = COLORS["gold"] if player.is_active_player else team_color
        outer = tk.Frame(parent, bg=border_color, padx=1, pady=1)
        outer.grid(row=0, column=slot, sticky="nsew", padx=3)
        card = tk.Frame(outer, bg=COLORS["panel_2"], padx=9, pady=8)
        card.pack(fill="both", expand=True)

        profile = self.player_profiles.get(player.riot_id)
        available = bool(profile and profile.status in {"OK", "LOCAL_ONLY", "PARTIAL"})
        partial = bool(profile and profile.status == "PARTIAL")
        duo_visual = duo_group_visuals(
            self.live_game.players, self.duo_pairs,
            active_team=self.live_game.active_team,
        ).get(player.riot_id)

        top = tk.Frame(card, bg=COLORS["panel_2"])
        top.pack(fill="x")
        role = self._position_text(player.position)
        tk.Label(
            top, text=role, bg=team_color, fg="#07101b", padx=7, pady=2,
            font=("Malgun Gothic", 7, "bold"),
        ).pack(side="left")
        pick_text, pick_color = self._live_pick_relation(player)
        if pick_text:
            tk.Label(
                top, text=pick_text, bg=COLORS["surface"], fg=pick_color,
                padx=6, pady=2, font=("Malgun Gothic", 7, "bold"),
            ).pack(side="left", padx=(5, 0))
        if duo_visual:
            duo_group, duo_color, _partner, _level, _evidence = duo_visual
            tk.Label(
                top, text=self._text("play.duo_badge", group=duo_group), bg=duo_color,
                fg="#07101b", padx=6, pady=2,
                font=("Malgun Gothic", 7, "bold"),
            ).pack(side="right")
        elif available and profile:
            tk.Label(
                top, text=f"{profile.season_wins}W - {profile.season_losses}L",
                bg=COLORS["panel_2"], fg=team_color, font=("Malgun Gothic", 8, "bold"),
            ).pack(side="right")

        display_name = player.riot_id if len(player.riot_id) <= 20 else player.riot_id[:19] + "…"
        player_name_label = tk.Label(
            card, text=(
                display_name
                + (self._text("play.me_suffix") if player.is_active_player else "")
            ),
            bg=COLORS["panel_2"],
            fg=(
                COLORS["gold"] if player.is_active_player
                else duo_visual[1] if duo_visual else COLORS["blue"]
            ),
            font=("Malgun Gothic", 9, "bold"), cursor="hand2",
        )
        player_name_label.pack(pady=(6, 1))
        player_name_label.bind(
            "<Button-1>",
            lambda _event, value=player.riot_id: self._open_player_history_tab(value),
            add="+",
        )

        identity = tk.Frame(card, bg=COLORS["panel_2"])
        identity.pack(fill="x", pady=(1, 6))
        icon_label = tk.Label(
            identity, text=self._champion_text(
                player.champion_id, player.champion_name_ko,
            )[:1], width=68, height=3,
            bg=COLORS["chip"], fg=COLORS["gold"], font=("Malgun Gothic", 16, "bold"),
            highlightthickness=2 if duo_visual else 1,
            highlightbackground=duo_visual[1] if duo_visual else border_color,
        )
        icon_label.pack(side="left", padx=(0, 8))
        icon = self.icon_cache.get(
            player.champion_id, 68,
            lambda label=icon_label, champion_id=player.champion_id:
            self._update_player_card_icon(label, champion_id, 68),
        )
        if icon:
            self._configure_player_card_icon(icon_label, icon)
        identity_text = tk.Frame(identity, bg=COLORS["panel_2"])
        identity_text.pack(side="left", fill="both", expand=True)
        tk.Label(
            identity_text, text=self._champion_text(
                player.champion_id, player.champion_name_ko,
            ), bg=COLORS["panel_2"],
            fg=COLORS["text"], font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")

        self._render_duo_group_strip(card, duo_visual)

        if not available or not profile:
            status_text = self._text("play.status_loading")
            if profile and profile.status == "NO_LOCAL_DATA":
                status_text = self._tr("OP.GG 시즌 전적 확인 중")
            elif profile and profile.status == "PRIVATE_OR_UNAVAILABLE":
                status_text = self._text("play.status_private")
            elif profile and profile.status not in {"NO_DATA", "LOADING"}:
                status_text = profile.status
            tk.Label(
                identity_text, text=self._tr("랭크 --"), bg=COLORS["panel_2"], fg=COLORS["muted"],
                font=("Malgun Gothic", 7),
            ).pack(anchor="w", pady=(2, 0))
            self._render_lane_matchup_strip(card, player)
            tk.Label(
                card, text=status_text, bg=COLORS["surface"], fg=COLORS["muted"],
                padx=6, pady=18, font=("Malgun Gothic", 8),
            ).pack(fill="x", pady=(0, 6))
            self._winrate_bar(card, None, self._tr("시즌 데이터 대기"))
            return

        rank_text = self._text("play.rank_unranked")
        if profile.tier != "UNRANKED":
            rank_text = f"{profile.tier} {profile.rank} · {profile.league_points}LP"
        rank_color = {
            "IRON": "#8c8a87", "BRONZE": "#b8794c", "SILVER": "#b8c4cf",
            "GOLD": COLORS["gold"], "PLATINUM": "#54d6c0", "EMERALD": "#42d68a",
            "DIAMOND": "#72a8ff", "MASTER": COLORS["purple"],
            "GRANDMASTER": COLORS["red"], "CHALLENGER": "#62d7ff",
        }.get(profile.tier, COLORS["muted"])
        tk.Label(
            identity_text, text=rank_text, bg=COLORS["panel_2"], fg=rank_color,
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w", pady=(2, 0))
        if duo_visual:
            tk.Label(
                identity_text,
                text=self._text(
                    "play.season_record", wins=profile.season_wins,
                    losses=profile.season_losses,
                ),
                bg=COLORS["panel_2"], fg=team_color, font=("Malgun Gothic", 7, "bold"),
            ).pack(anchor="w", pady=(2, 0))

        self._render_recent_form_badges(card, player, profile)
        self._render_player_tendency_badges(card, player)
        self._render_lane_matchup_strip(card, player)

        stats = tk.Frame(card, bg=COLORS["panel_2"])
        stats.pack(fill="x", pady=(0, 5))
        stats.grid_columnconfigure(0, weight=1, uniform="compact_stats")
        stats.grid_columnconfigure(1, weight=1, uniform="compact_stats")
        champion_losses = max(profile.champion_games - profile.champion_wins, 0)
        if profile.champion_data_source == "OPGG":
            champion_value = (
                self._text(
                    "play.champion_record", games=profile.champion_games,
                    rate=_fmt_rate(profile.champion_win_rate),
                )
            )
            champion_detail = self._text(
                "play.champion_source_opgg", wins=profile.champion_wins,
                losses=champion_losses,
            )
            sample_color = (
                COLORS["orange"] if profile.champion_games < 5 else COLORS["green"]
            )
        elif profile.champion_data_source == "OPGG_NOT_LISTED":
            champion_value = self._text("play.opgg_not_listed")
            champion_detail = self._text("play.opgg_not_listed_detail")
            sample_color = COLORS["muted"]
        elif profile.champion_data_source == "RIOT_LIVE":
            champion_value = (
                self._text(
                    "play.champion_record", games=profile.champion_games,
                    rate=_fmt_rate(profile.champion_win_rate),
                ) if profile.champion_games else self._text("play.current_no_selection")
            )
            champion_detail = self._text(
                "play.champion_source_riot", loaded=profile.local_sample_games,
                target=profile.champion_sample_target,
                wins=profile.champion_wins, losses=champion_losses,
            )
            sample_color = (
                COLORS["orange"]
                if profile.local_sample_games < profile.champion_sample_target
                or 0 < profile.champion_games < 3
                else COLORS["green"] if profile.champion_games else COLORS["blue"]
            )
        elif profile.champion_data_source == "LOCAL":
            champion_value = (
                self._text(
                    "play.champion_record", games=profile.champion_games,
                    rate=_fmt_rate(profile.champion_win_rate),
                ) if profile.champion_games else self._text("play.current_none")
            )
            champion_detail = self._text(
                "play.champion_source_local", sample=profile.local_sample_games,
                wins=profile.champion_wins, losses=champion_losses,
            )
            sample_color = (
                COLORS["orange"] if 0 < profile.champion_games < 5 else COLORS["green"]
            )
        else:
            champion_value = self._text("play.opgg_checking")
            champion_detail = self._text("play.opgg_fallback")
            sample_color = COLORS["blue"]
        if partial and profile.champion_data_source not in {
            "RIOT_LIVE", "OPGG", "OPGG_NOT_LISTED"
        }:
            champion_value = (
                self._text("play.opgg_checking")
                if not player.is_active_player else self._tr("계산 중")
            )
            champion_detail = (
                self._tr("시즌 챔피언 전적 요청 중")
                if not player.is_active_player else self._tr("내 저장 전적 계산 중")
            )
        self._compact_stat(
            stats, 0, self._tr("현 챔프"), champion_value,
            champion_detail, sample_color,
        )

        if profile.last_game_champion_id:
            last_result = self._tr("승" if profile.last_game_won else "패")
            kda_text = "Perfect" if profile.last_game_deaths == 0 else f"{profile.last_game_kda:.1f}"
            last_value = f"{last_result} · KDA {kda_text}"
            last_detail = (
                f"{self._champion_text(profile.last_game_champion_id)}  "
                f"{profile.last_game_kills}/{profile.last_game_deaths}/{profile.last_game_assists}"
            )
            last_color = COLORS["green"] if profile.last_game_won else COLORS["red"]
        elif partial:
            last_value, last_detail, last_color = (
                self._tr("계산 중"), self._tr("상세 기록"), COLORS["blue"]
            )
        else:
            last_value, last_detail, last_color = (
                self._tr("데이터 없음"), self._tr("솔로랭크"), COLORS["muted"]
            )
        self._compact_stat(stats, 1, self._tr("전판"), last_value, last_detail, last_color)
        self._render_recent_form_summary(card, player, profile)

        if partial:
            relationship = self._text("play.relationship_loading")
        elif player.is_active_player:
            relationship = self._text("play.relationship_me")
        else:
            relation_parts: list[str] = []
            if profile.together_games:
                relation_parts.append(
                    self._text(
                        "play.relationship_team", games=profile.together_games,
                        rate=_fmt_rate(profile.together_win_rate),
                    )
                )
            if profile.against_games:
                relation_parts.append(
                    self._text(
                        "play.relationship_enemy", games=profile.against_games,
                        rate=_fmt_rate(profile.against_my_win_rate),
                    )
                )
            relationship = self._text(
                "play.relationship", record=(
                    " / ".join(relation_parts)
                    if relation_parts else self._text("play.relationship_none")
                ),
            )
        tk.Label(
            card, text=relationship, bg=COLORS["panel_2"], fg=COLORS["purple"],
            font=("Malgun Gothic", 7, "bold"), anchor="w",
        ).pack(fill="x")

        if partial:
            meeting = self._text("play.recent_meeting_loading")
        elif profile.last_met_game_number:
            when = (
                self._text("play.previous_match")
                if profile.last_met_game_number == 1 else self._text(
                    "play.recent_index", index=profile.last_met_game_number,
                )
            )
            side = self._text(
                "play.same_team" if profile.last_met_same_team else "play.opponent"
            )
            result = self._tr("승" if profile.last_met_my_win else "패")
            meeting = self._text(
                "play.recent_meeting", when=when, side=side, result=result,
            )
        else:
            meeting = self._text("play.recent_meeting_none")
        tk.Label(
            card, text=meeting, bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7), anchor="w",
        ).pack(fill="x", pady=(2, 0))

        if not duo_visual:
            tk.Label(
                card,
                text="DUO 신호 없음 · 진행 상태는 상단 표시",
                bg=COLORS["panel_2"],
                fg="#65738a",
                font=("Malgun Gothic", 7), anchor="w",
            ).pack(fill="x", pady=(2, 0))

        self._winrate_bar(
            card, profile.season_win_rate,
            self._text(
                "play.season_bar", wins=profile.season_wins,
                losses=profile.season_losses,
            ),
        )

    def _render_recent_form_badges(
        self, parent: tk.Widget, player: LivePlayer, profile: PlayerProfileStat,
    ) -> None:
        badges: list[tuple[str, str]] = []
        overall = self._streak_text(profile.overall_streak)
        champion = self._streak_text(
            profile.champion_streak,
            f"{self._champion_text(player.champion_id, player.champion_name_ko)} ",
        )
        if overall:
            badges.append((
                f"{'🔥' if profile.overall_streak > 0 else '❄'} {overall}",
                COLORS["green"] if profile.overall_streak > 0 else COLORS["red"],
            ))
        if champion:
            badges.append((
                champion,
                COLORS["blue"] if profile.champion_streak > 0 else COLORS["orange"],
            ))
        if not badges and profile.recent_games:
            losses = profile.recent_games - profile.recent_wins
            recent_rate = profile.recent_win_rate
            color = (
                COLORS["green"] if recent_rate is not None and recent_rate >= 60
                else COLORS["red"] if recent_rate is not None and recent_rate <= 40
                else COLORS["blue"]
            )
            badges.append((self._text(
                "play.recent_form", games=profile.recent_games,
                wins=profile.recent_wins, losses=losses,
            ), color))
        if not badges:
            return
        row = tk.Frame(parent, bg=COLORS["panel_2"])
        row.pack(fill="x", pady=(0, 5))
        for text, color in badges[:2]:
            tk.Label(
                row, text=text, bg=BUTTON_FILLS.get(color, COLORS["chip"]), fg=color,
                padx=6, pady=2, font=("Malgun Gothic", 7, "bold"),
            ).pack(side="left", padx=(0, 5))

    def _render_player_tendency_badges(
        self, parent: tk.Widget, player: LivePlayer,
    ) -> None:
        """Show evidence-backed behavior chips directly on the player card."""
        labels: list[str] = []
        behavior = getattr(self, "player_behaviors", {}).get(player.riot_id)
        if behavior and behavior.status == "OK":
            labels.extend(behavior.labels)
        stat = getattr(self, "jungle_tendencies", {}).get(player.riot_id)
        if stat and stat.status in {"OK", "SUMMARY"}:
            labels.extend(stat.labels)
        labels = list(dict.fromkeys(labels))
        if not labels:
            return
        priority = {
            "데스 많음": 0, "초반 라인 약세": 1, "시야 부족": 2,
            "군중 통제 강함": 3, "좋은 탱킹": 4, "회복·보호 강함": 5,
            "갱킹 자주 감": 6, "퍼블을 자주 땀": 7,
            "공격적 딜링": 8, "오브젝트 기여": 9, "철거 기여": 10,
        }
        labels.sort(key=lambda label: (priority.get(label, 50), label))
        row = tk.Frame(parent, bg=COLORS["panel_2"])
        row.pack(fill="x", pady=(0, 5))
        negative = {
            "최근 정글 폼 부진", "초반 갱 적음", "데스 주의", "데스 많음",
            "초반 교전 적음", "초반 라인 약세", "합류 낮음", "시야 부족",
            "제어 와드 부족",
        }
        positive = {
            "최근 정글 폼 우세", "최근 KDA 안정", "생존 안정", "시야 좋음",
            "초반 라인 강함", "합류 잦음", "제어 와드 적극",
            "갱킹 자주 감", "카정 잦음", "오브젝트 즉시",
            "퍼블을 자주 땀", "퍼블 관여 높음", "초반 교전 잦음",
            "군중 통제 강함", "좋은 탱킹", "회복·보호 강함",
            "공격적 딜링", "오브젝트 기여", "철거 기여",
        }
        for index, text in enumerate(labels[:6]):
            color = COLORS["red"] if text in negative else COLORS["orange"]
            if text in positive:
                color = COLORS["green"]
            tk.Label(
                row, text=self._tr(text), bg=COLORS["chip"], fg=color,
                padx=6, pady=2, font=("Malgun Gothic", 7, "bold"),
            ).grid(
                row=index // 3, column=index % 3, sticky="w",
                padx=(0, 5), pady=(0, 3),
            )

    def _render_recent_form_summary(
        self, parent: tk.Widget, player: LivePlayer, profile: PlayerProfileStat,
    ) -> None:
        if not profile.recent_form_source:
            return
        panel = tk.Frame(
            parent, bg=COLORS["surface"], padx=7, pady=5,
            highlightthickness=1, highlightbackground=COLORS["divider"],
        )
        panel.pack(fill="x", pady=(0, 5))
        if not profile.recent_games:
            tk.Label(
                panel, text="최근 폼 · 완료된 솔로랭크 경기 없음",
                bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Malgun Gothic", 7), anchor="w",
            ).pack(fill="x")
            return

        losses = profile.recent_games - profile.recent_wins
        win_rate = profile.recent_win_rate or 0.0
        form_color = (
            COLORS["green"] if win_rate >= 60.0
            else COLORS["red"] if win_rate <= 40.0
            else COLORS["blue"]
        )
        details = [self._text(
            "play.recent_details", games=profile.recent_games,
            wins=profile.recent_wins, losses=losses, rate=win_rate,
            kda=(profile.recent_kda or 0.0),
        )]
        if profile.recent_op_score > 0:
            details.append(f"OP {profile.recent_op_score:.1f}")
        if profile.last_op_score_rank > 0:
            details.append(
                f"Previous rank {profile.last_op_score_rank}"
                if self.ui_language == "en"
                else f"전판 {profile.last_op_score_rank}등"
            )
        tk.Label(
            panel, text=" · ".join(details), bg=COLORS["surface"], fg=form_color,
            font=("Malgun Gothic", 7, "bold"), anchor="w",
        ).pack(fill="x")

        if profile.champion_recent_games:
            champion_losses = profile.champion_recent_games - profile.champion_recent_wins
            champion_rate = (
                profile.champion_recent_wins / profile.champion_recent_games * 100
            )
            tk.Label(
                panel,
                text=(
                    self._text(
                        "play.champion_recent",
                        champion=self._champion_text(
                            player.champion_id, player.champion_name_ko,
                        ),
                        games=profile.champion_recent_games,
                        wins=profile.champion_recent_wins,
                        losses=champion_losses,
                        rate=champion_rate,
                    )
                ),
                bg=COLORS["surface"], fg="#b9c6dc",
                font=("Malgun Gothic", 6, "bold"), anchor="w",
            ).pack(fill="x", pady=(2, 0))

    @staticmethod
    def _comparable_live_position(position: str) -> str:
        return "SUPPORT" if position in {"UTILITY", "SUPPORT"} else position

    @classmethod
    def _live_lane_pairs(
        cls, snapshot: LiveGameSnapshot,
    ) -> list[tuple[str, LivePlayer, LivePlayer]]:
        allies = {
            cls._comparable_live_position(player.position): player
            for player in snapshot.allies
            if cls._comparable_live_position(player.position) != "UNKNOWN"
            and player.champion_id
        }
        enemies = {
            cls._comparable_live_position(player.position): player
            for player in snapshot.enemies
            if cls._comparable_live_position(player.position) != "UNKNOWN"
            and player.champion_id
        }
        order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "SUPPORT": 4}
        return [
            (position, allies[position], enemies[position])
            for position in sorted(allies.keys() & enemies.keys(), key=lambda item: order.get(item, 9))
        ]

    @staticmethod
    def _empty_lane_matchup(
        position: str, ally: LivePlayer, enemy: LivePlayer,
    ) -> LaneMatchupStat:
        return LaneMatchupStat(
            position=position,
            ally_champion_id=ally.champion_id,
            ally_champion_name_ko=ally.champion_name_ko,
            enemy_champion_id=enemy.champion_id,
            enemy_champion_name_ko=enemy.champion_name_ko,
        )

    def _prepare_live_lane_matchups(
        self, snapshot: LiveGameSnapshot, signature: str,
    ) -> None:
        pairs = self._live_lane_pairs(snapshot)
        self.lane_matchups = {}
        pending: list[tuple[str, LivePlayer, LivePlayer]] = []
        for position, ally, enemy in pairs:
            cached = self.storage.load_opgg_snapshot(enemy.champion_id, position)
            if cached:
                self.lane_matchups[position] = lane_matchup_from_snapshot(
                    position,
                    ally.champion_id,
                    ally.champion_name_ko,
                    enemy.champion_id,
                    enemy.champion_name_ko,
                    cached,
                    cached=True,
                )
            else:
                self.lane_matchups[position] = self._empty_lane_matchup(
                    position, ally, enemy
                )
            if not cached or not self._matchup_snapshot_fresh(cached):
                pending.append((position, ally, enemy))

        active_position = next(
            (
                self._comparable_live_position(player.position)
                for player in snapshot.players if player.is_active_player
            ),
            "UNKNOWN",
        )
        order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "SUPPORT": 4}
        pending.sort(key=lambda item: (item[0] != active_position, order.get(item[0], 9)))
        self._refresh_live_lane_matchups(pending, signature)

    def _refresh_live_lane_matchups(
        self,
        pending: list[tuple[str, LivePlayer, LivePlayer]],
        signature: str,
    ) -> None:
        if not pending or signature in self._lane_matchup_refreshing:
            return
        self._lane_matchup_refreshing.add(signature)

        def work() -> None:
            for position, ally, enemy in pending:
                try:
                    snapshot = self.opgg_client.refresh_matchup(
                        enemy.champion_id, position
                    )
                    self.storage.save_opgg_snapshot(snapshot)
                    stat = lane_matchup_from_snapshot(
                        position,
                        ally.champion_id,
                        ally.champion_name_ko,
                        enemy.champion_id,
                        enemy.champion_name_ko,
                        snapshot,
                        cached=False,
                    )
                except Exception as exc:
                    self._post_ui(
                        lambda lane=position, captured=exc:
                        self._apply_lane_matchup_error(lane, captured, signature)
                    )
                    continue
                self._post_ui(
                    lambda lane=position, value=stat:
                    self._apply_lane_matchup(lane, value, signature)
                )

        def success(_result: None) -> None:
            self._lane_matchup_refreshing.discard(signature)
            if signature == self._live_signature:
                self._schedule_play_render()

        def error(exc: Exception) -> None:
            self._lane_matchup_refreshing.discard(signature)
            if signature == self._live_signature:
                self.live_profile_status.configure(
                    text=f"OP.GG 라인 상성 갱신 실패 · {exc}", fg=COLORS["orange"]
                )
                self._schedule_play_render()

        self._background(work, success, error)

    def _apply_lane_matchup(
        self, position: str, stat: LaneMatchupStat, signature: str,
    ) -> None:
        if signature != self._live_signature:
            return
        self.lane_matchups[position] = stat
        self._schedule_play_render()

    def _apply_lane_matchup_error(
        self, position: str, exc: Exception, signature: str,
    ) -> None:
        if signature != self._live_signature:
            return
        current = self.lane_matchups.get(position)
        if not current:
            return
        if current.ally_win_rate is not None:
            self.lane_matchups[position] = replace(
                current, status="CACHE", cached=True, message="새 통계 갱신 실패"
            )
        else:
            self.lane_matchups[position] = replace(
                current, status="ERROR", message=str(exc)
            )
        self._schedule_play_render()

    def _render_lane_matchup_strip(
        self, parent: tk.Widget, player: LivePlayer,
    ) -> None:
        position = self._comparable_live_position(player.position)
        stat = getattr(self, "lane_matchups", {}).get(position)
        outer = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        outer.pack(fill="x", pady=(0, 6))
        frame = tk.Frame(outer, bg=COLORS["surface"], padx=7, pady=5)
        frame.pack(fill="both", expand=True)
        if not stat:
            text = "OP.GG 맞대결 · 포지션 확인 중" if position == "UNKNOWN" else "OP.GG 맞대결 · 확인 중…"
            tk.Label(
                frame, text=text, bg=COLORS["surface"], fg=COLORS["blue"],
                font=("Malgun Gothic", 7, "bold"), anchor="w",
            ).pack(fill="x")
            return

        ally_perspective = player.team == self.live_game.active_team
        player_rate = stat.ally_win_rate if ally_perspective else stat.enemy_win_rate
        opponent_name = (
            stat.enemy_champion_name_ko if ally_perspective
            else stat.ally_champion_name_ko
        )
        player_name = (
            stat.ally_champion_name_ko if ally_perspective
            else stat.enemy_champion_name_ko
        )
        if player_rate is None:
            status_text = {
                "LOADING": "확인 중…",
                "ERROR": "갱신 실패",
                "NO_DATA": "비교 표본 없음",
            }.get(stat.status, "데이터 없음")
            tk.Label(
                frame, text=f"OP.GG 맞대결 · {status_text}",
                bg=COLORS["surface"],
                fg=COLORS["orange"] if stat.status == "ERROR" else COLORS["blue"],
                font=("Malgun Gothic", 7, "bold"), anchor="w",
            ).pack(fill="x")
            tk.Label(
                frame, text=f"{player_name} vs {opponent_name}",
                bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Malgun Gothic", 6), anchor="w",
            ).pack(fill="x", pady=(2, 0))
            return

        opponent_rate = round(100.0 - player_rate, 2)
        label = lane_matchup_label(player_rate)
        color = (
            COLORS["green"] if player_rate >= 51.5 else
            COLORS["red"] if player_rate < 48.5 else COLORS["gold"]
        )
        tk.Label(
            frame,
            text=(
                f"{player_name} vs {opponent_name} · "
                f"게임 {player_rate:.1f}% : {opponent_rate:.1f}%"
            ),
            bg=COLORS["surface"], fg=color, font=("Malgun Gothic", 7, "bold"),
            anchor="w", justify="left", wraplength=250,
        ).pack(fill="x")
        player_laning_rate = (
            stat.ally_laning_win_rate if ally_perspective
            else stat.enemy_laning_win_rate
        )
        opponent_laning_rate = (
            stat.enemy_laning_win_rate if ally_perspective
            else stat.ally_laning_win_rate
        )
        lane_text = (
            f"라인전 {player_laning_rate:.1f}% : {(opponent_laning_rate or 0.0):.1f}%"
            if player_laning_rate is not None else "라인전 · 소스 미제공"
        )
        tk.Label(
            frame, text=f"{lane_text} · {label}", bg=COLORS["surface"],
            fg=COLORS["purple"] if player_laning_rate is not None else COLORS["muted"],
            font=("Malgun Gothic", 6, "bold"), anchor="w",
        ).pack(fill="x", pady=(2, 0))
        details = ["OP.GG 게임 승률"]
        if stat.games:
            details.append(f"{stat.games:,}게임")
        if stat.patch and stat.patch != "UNKNOWN":
            details.append(f"패치 {stat.patch}")
        if stat.cached:
            details.append("캐시")
        if stat.message:
            details.append(stat.message)
        tk.Label(
            frame, text=" · ".join(details), bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Malgun Gothic", 6), anchor="w",
        ).pack(fill="x", pady=(2, 0))

    def _live_pick_relation(self, player: LivePlayer) -> tuple[str, str]:
        turn = player.draft_pick_turn
        team_order = player.draft_team_pick_order
        if turn is None:
            return "", COLORS["muted"]
        position = self._comparable_live_position(player.position)
        opponent = next(
            (
                other for other in self.live_game.players
                if other.team != player.team
                and self._comparable_live_position(other.position) == position
                and other.draft_pick_turn is not None
            ),
            None,
        )
        relation = ""
        color = COLORS["blue"]
        if opponent:
            if turn < int(opponent.draft_pick_turn):
                relation, color = "선픽", COLORS["orange"]
            elif turn > int(opponent.draft_pick_turn):
                relation, color = "후픽", COLORS["green"]
            else:
                relation, color = "동시 픽", COLORS["blue"]
        order_text = f"팀 {team_order}픽" if team_order is not None else "팀 순서 미확인"
        prefix = f"{relation} · " if relation else ""
        return f"{prefix}전체 {turn}턴 ({order_text})", color

    def _compact_stat(
        self, parent: tk.Widget, column: int, title: str, value: str, detail: str, color: str
    ) -> None:
        frame = tk.Frame(parent, bg=COLORS["surface"], padx=6, pady=5)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 3) if column == 0 else (3, 0))
        tk.Label(
            frame, text=title, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 7, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame, text=value, bg=COLORS["surface"], fg=color,
            font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame, text=detail, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 6),
        ).pack(anchor="w")

    def _winrate_bar(self, parent: tk.Widget, rate: float | None, label: str) -> None:
        panel = tk.Frame(
            parent, bg=COLORS["surface"], padx=7, pady=5,
            highlightthickness=1, highlightbackground=COLORS["divider"],
        )
        panel.pack(fill="x", pady=(7, 0))
        header = tk.Frame(panel, bg=COLORS["surface"])
        header.pack(fill="x")
        tk.Label(
            header, text=label, bg=COLORS["surface"], fg="#b9c6dc",
            font=("Malgun Gothic", 7, "bold"), anchor="w",
        ).pack(side="left")
        rate_color = (
            COLORS["green"] if rate is not None and rate >= 50.0
            else COLORS["red"] if rate is not None else COLORS["muted"]
        )
        tk.Label(
            header,
            text=f"{rate:.1f}%" if rate is not None else "--",
            bg=COLORS["surface"], fg=rate_color,
            font=("Malgun Gothic", 9, "bold"), anchor="e",
        ).pack(side="right")
        canvas = tk.Canvas(
            panel, height=9, bg="#351a24", highlightthickness=0, bd=0,
        )
        canvas.pack(fill="x", pady=(4, 0))

        def draw(_event: tk.Event | None = None) -> None:
            width = max(canvas.winfo_width(), 10)
            canvas.delete("all")
            canvas.create_rectangle(
                0, 0, width, 9,
                fill="#7a2737" if rate is not None else "#273449",
                outline="",
            )
            if rate is not None:
                filled = int(width * max(0.0, min(rate, 100.0)) / 100.0)
                canvas.create_rectangle(
                    0, 0, filled, 9, fill="#27b980", outline=""
                )
                canvas.create_line(filled, 0, filled, 9, fill="#07101b", width=1)

        canvas.bind("<Configure>", draw)
        canvas.after_idle(draw)

    def _ensure_history_loaded(self, force: bool = False) -> None:
        if self._history_loading:
            if force:
                self._history_reload_requested = True
            return
        if getattr(self, "demo", False):
            if self.history_overview is None:
                self.history_overview = self._demo_history_overview()
            self._history_revision = self.storage.match_revision()
            self._render_history()
            return
        puuid = self._history_puuid()
        if not puuid:
            self.history_overview = HistoryOverview()
            self.history_status_label.configure(
                text="Riot 설정 후 전적을 갱신하세요.", fg=COLORS["orange"]
            )
            self._render_history()
            return
        revision = self.storage.match_revision()
        if not force and self.history_overview is not None and revision == self._history_revision:
            self._render_history()
            return
        self._history_loading = True
        self.history_status_label.configure(text="최대 1,000경기 분석 중…", fg=COLORS["blue"])
        self._render_history()

        def work() -> HistoryOverview:
            matches = self.storage.player_matches(puuid, limit=1000)
            overview = analyze_history(matches, puuid)
            # LP and prediction accuracy are optional enrichments. A damaged
            # legacy row must never hide the underlying Riot match history.
            try:
                attach_match_lp_changes(
                    overview,
                    self.storage.load_match_lp_changes(
                        entry.match_id for entry in overview.entries
                    ),
                )
            except Exception:
                pass
            try:
                self.storage.resolve_game_predictions(matches[:50])
                predictions = self.storage.load_game_predictions(
                    entry.match_id for entry in overview.entries
                )
            except Exception:
                predictions = {}
            for entry in overview.entries:
                prediction = predictions.get(entry.match_id)
                if not prediction:
                    continue
                entry.predicted_win_rate = prediction.win_probability
                entry.predicted_win = prediction.predicted_win
                entry.prediction_confidence = prediction.confidence
                entry.prediction_correct = prediction.correct
            return overview

        def success(overview: HistoryOverview) -> None:
            self._history_loading = False
            self.history_overview = overview
            self._history_revision = revision
            self._history_visible_count = 10
            self._render_history()
            if self._history_reload_requested:
                self._history_reload_requested = False
                self.root.after(50, lambda: self._ensure_history_loaded(force=True))

        def error(exc: Exception) -> None:
            self._history_loading = False
            self._history_reload_requested = False
            self.history_status_label.configure(text=f"전적 분석 실패 · {exc}", fg=COLORS["red"])

        self._background(work, success, error)

    def _history_puuid(self) -> str:
        game_name = self.storage.get_setting("riot_game_name")
        tag_line = self.storage.get_setting("riot_tag_line")
        riot_id = f"{game_name}#{tag_line}" if game_name and tag_line else ""
        return (
            self.storage.find_puuid_by_riot_id(riot_id) if riot_id else ""
        ) or self.storage.get_setting("riot_puuid")

    def _render_history(self) -> None:
        if not hasattr(self, "history_matches_frame"):
            return
        if self._current_main_tab_index() != 2:
            return
        if not self._history_home_is_selected():
            return
        game_name = self.storage.get_setting("riot_game_name") or "Riot 계정 미확인"
        tag_line = self.storage.get_setting("riot_tag_line")
        signature = repr((
            self._history_asset_revision,
            game_name,
            tag_line,
            self.history_overview,
            self._history_loading,
            self._history_result_filter,
            self._history_position_filter,
            self._history_visible_count,
            self._history_rank_text(game_name, tag_line)
            if self.history_overview is not None else None,
        ))
        if signature == self._history_content_signature:
            return
        self._history_content_signature = signature
        overview = self.history_overview
        champion_signature = repr((
            self._history_asset_revision,
            tuple(overview.champions) if overview else (),
            self._history_position_filter,
        ))
        matches_signature = repr((
            self._history_asset_revision,
            tuple(overview.entries) if overview else (),
            self._history_result_filter,
            self._history_visible_count,
            self._history_loading if overview is None else None,
        ))
        render_champions = champion_signature != self._history_champion_signature
        render_matches = matches_signature != self._history_matches_signature
        if render_champions:
            self._history_champion_signature = champion_signature
            self._clear(self.history_champions_frame)
        if render_matches:
            self._history_matches_signature = matches_signature
            self._clear(self.history_matches_frame)
        self.history_identity_label.configure(
            text=f"{game_name}{'#' + tag_line if tag_line else ''}"
        )
        if overview is None:
            message = "로컬 전적 분석 중…" if self._history_loading else "내 전적 탭을 열면 자동 분석합니다."
            if render_matches:
                tk.Label(
                    self.history_matches_frame, text=message, bg=COLORS["surface"],
                    fg=COLORS["muted"], padx=14, pady=35, font=("Malgun Gothic", 9),
                ).pack(fill="x")
            return

        rank_value, rank_detail = self._history_rank_text(game_name, tag_line)
        self.history_metrics["rank"][0].configure(text=rank_value)
        self.history_metrics["rank"][1].configure(text=rank_detail)
        self.history_metrics["games"][0].configure(
            text=self._text("history.games", count=overview.games)
        )
        self.history_metrics["games"][1].configure(
            text=self._text(
                "history.record", wins=overview.wins,
                losses=overview.games - overview.wins,
                rate=self._tr(_fmt_rate(overview.win_rate)),
            )
        )
        streak = (
            self._text("history.win_streak", count=overview.current_streak)
            if overview.current_streak > 0 else
            self._text("history.loss_streak", count=abs(overview.current_streak))
            if overview.current_streak < 0 else self._text("history.no_streak")
        )
        self.history_metrics["recent"][0].configure(
            text=self._tr(_fmt_rate(overview.recent_20_win_rate))
        )
        self.history_metrics["recent"][1].configure(
            text=self._text(
                "history.recent_record", wins=overview.recent_20_wins,
                losses=overview.recent_20_games - overview.recent_20_wins,
                streak=streak,
            )
        )
        lp_sum, lp_games, lp_window = recent_exact_lp_summary(overview.entries)
        if lp_sum is None:
            self.history_lp_strip_label.configure(
                text="LP 변동은 새 솔로랭크부터 정확히 기록합니다.",
                fg=COLORS["muted"],
            )
        else:
            lp_color = (
                COLORS["green"] if lp_sum > 0
                else COLORS["red"] if lp_sum < 0 else COLORS["gold"]
            )
            self.history_lp_strip_label.configure(
                text=(
                    self._text(
                        "history.lp_summary", delta=lp_sum,
                        known=lp_games, window=lp_window,
                    )
                ),
                fg=lp_color,
            )
        recent_champion_signature = repr(tuple(
            (stat.champion_id, stat.games, stat.wins)
            for stat in overview.recent_20_champions[:3]
        ))
        if recent_champion_signature != self._history_recent_champions_signature:
            self._history_recent_champions_signature = recent_champion_signature
            self._clear(self.history_recent_champions_frame)
            for stat in overview.recent_20_champions[:3]:
                chip = tk.Frame(
                    self.history_recent_champions_frame,
                    bg=COLORS["surface"], padx=5, pady=2,
                )
                chip.pack(side="left", padx=(3, 0))
                icon_label = tk.Label(
                    chip, text=self._champion_text(stat.champion_id)[:1],
                    bg=COLORS["chip"], fg=COLORS["gold"], width=22,
                    font=("Malgun Gothic", 7, "bold"),
                )
                icon_label.pack(side="left", padx=(0, 4))

                def apply_recent_icon(
                    label: tk.Label = icon_label,
                    champion_id: str = stat.champion_id,
                ) -> None:
                    try:
                        image_value = self.icon_cache.get(champion_id, 22)
                        if label.winfo_exists() and image_value:
                            label.configure(image=image_value, text="", width=0)
                    except tk.TclError:
                        return

                image = self.icon_cache.get(stat.champion_id, 22, apply_recent_icon)
                if image:
                    icon_label.configure(image=image, text="", width=0)
                tk.Label(
                    chip,
                    text=self._text(
                        "history.champion_chip",
                        champion=self._champion_text(stat.champion_id),
                        games=stat.games,
                        rate=self._tr(_fmt_rate(stat.win_rate)),
                    ),
                    bg=COLORS["surface"], fg=COLORS["text"],
                    font=("Malgun Gothic", 7, "bold"),
                ).pack(side="left")
        self.history_metrics["kda"][0].configure(
            text=f"{overview.kda:.2f}" if overview.kda is not None else "--"
        )
        self.history_metrics["kda"][1].configure(
            text=self._text(
                "history.total_kda", kills=overview.kills,
                deaths=overview.deaths, assists=overview.assists,
            )
        )
        self.history_metrics["vision"][0].configure(
            text=f"{overview.average_vision:.1f}" if overview.average_vision is not None else "--"
        )
        self.history_metrics["vision"][1].configure(text=self._tr("경기당 시야 점수"))
        prediction_hits, prediction_total, prediction_rate = (
            recent_prediction_accuracy(overview.entries)
        )
        if prediction_rate is not None:
            self.history_metrics["prediction"][0].configure(
                text=f"{prediction_rate:.1f}%"
            )
            self.history_metrics["prediction"][1].configure(
                text=(
                    self._text(
                        "history.prediction_accuracy", hits=prediction_hits,
                        total=prediction_total,
                    )
                )
            )
        else:
            self.history_metrics["prediction"][0].configure(text="--")
            self.history_metrics["prediction"][1].configure(
                text=self._tr("새 게임부터 기록")
            )

        if getattr(self, "demo", False):
            self.history_status_label.configure(
                text=(
                    self._text("history.demo_status", games=overview.games)
                ),
                fg=COLORS["gold"],
            )
        elif self._history_loading:
            self.history_status_label.configure(text="새 로컬 데이터 분석 중…", fg=COLORS["blue"])
        else:
            self.history_status_label.configure(
                text=self._text("history.local_status", games=overview.games),
                fg=COLORS["green"],
            )

        if render_champions:
            for key, button in self.history_position_buttons.items():
                accent = (
                    COLORS["purple"] if key == "ALL"
                    else POSITION_BADGE_COLORS[key]
                )
                self._set_button_selected(
                    button, key == self._history_position_filter, accent
                )
            champion_rows = [
                stat for stat in overview.champions
                if self._history_position_filter == "ALL"
                or stat.position == self._history_position_filter
            ]
            if not champion_rows:
                tk.Label(
                    self.history_champions_frame,
                    text=(
                        "저장된 챔피언 기록이 없습니다."
                        if self._history_position_filter == "ALL"
                        else f"{position_name(self._history_position_filter)} 기록이 없습니다."
                    ),
                    bg=COLORS["panel_2"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
                ).pack(anchor="w", pady=10)
            for stat in champion_rows[:12]:
                self._render_history_champion(stat)

        if render_matches:
            if not overview.entries:
                tk.Label(
                    self.history_matches_frame,
                    text="저장된 솔로랭크 경기가 없습니다. Riot 전적 갱신을 눌러 먼저 저장하세요.",
                    bg=COLORS["surface"], fg=COLORS["muted"], padx=14, pady=35,
                    font=("Malgun Gothic", 9),
                ).pack(fill="x")
            entries = [
                entry for entry in overview.entries
                if self._history_result_filter == "ALL"
                or (self._history_result_filter == "WIN" and entry.won)
                or (self._history_result_filter == "LOSS" and not entry.won)
            ]
            for key, button in self.history_filter_buttons.items():
                accent = {
                    "ALL": COLORS["purple"], "WIN": COLORS["green"], "LOSS": COLORS["red"],
                }[key]
                self._set_button_selected(
                    button, key == self._history_result_filter, accent
                )
            for entry in entries[:self._history_visible_count]:
                self._render_history_match(entry)
            remaining = len(entries) - self._history_visible_count
            self.history_more_button.configure(
                state="normal" if remaining > 0 else "disabled",
                text=(
                    self._text("history.more", remaining=remaining)
                    if remaining > 0 else self._text("history.all_shown")
                ),
            )

    def _set_history_result_filter(self, result_filter: str) -> None:
        if result_filter not in {"ALL", "WIN", "LOSS"}:
            return
        self._history_result_filter = result_filter
        self._history_visible_count = 10
        self._render_history()

    def _set_history_position_filter(self, position_filter: str) -> None:
        if position_filter not in {
            "ALL", "TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT",
        }:
            return
        if position_filter == self._history_position_filter:
            return
        self._history_position_filter = position_filter
        self._history_champion_signature = ""
        self._render_history()

    def _history_rank_text(self, game_name: str, tag_line: str) -> tuple[str, str]:
        riot_id = f"{game_name}#{tag_line}" if tag_line else game_name
        opgg_profile = self.storage.load_opgg_player_profile_any_age(riot_id)
        if opgg_profile:
            if opgg_profile.tier == "UNRANKED":
                return "UNRANKED", self._tr("OP.GG · 솔로랭크 배치 전")
            return (
                f"{opgg_profile.tier} {opgg_profile.division}",
                f"{opgg_profile.league_points}LP · "
                + self._text(
                    "history.record", wins=opgg_profile.season_wins,
                    losses=opgg_profile.season_losses, rate="OP.GG",
                ),
            )
        cached = self.storage.load_live_profile_any_age(
            riot_id
        )
        if not cached:
            return self._tr("랭크 미확인"), self._tr("Riot 전적 갱신 필요")
        _puuid, payload, _updated_at = cached
        entry = payload.get("solo_entry") or {}
        tier = str(entry.get("tier") or "UNRANKED")
        if tier == "UNRANKED":
            return "UNRANKED", self._tr("솔로랭크 배치 전")
        wins = int(entry.get("wins") or 0)
        losses = int(entry.get("losses") or 0)
        return (
            f"{tier} {entry.get('rank') or ''}",
            f"{entry.get('leaguePoints') or 0}LP · "
            + self._text("history.record", wins=wins, losses=losses, rate="Riot"),
        )

    def _render_history_champion(self, stat: object) -> None:
        champion_id = str(getattr(stat, "champion_id", "Unknown"))
        position = self._comparable_live_position(
            str(getattr(stat, "position", "UNKNOWN"))
        )
        outer = tk.Frame(
            self.history_champions_frame, bg=COLORS["surface"], padx=8, pady=6,
        )
        outer.pack(fill="x", pady=2)
        icon_label = tk.Label(
            outer, text=self._champion_text(champion_id)[:1],
            bg=COLORS["chip"], fg=COLORS["gold"], width=34,
            font=("Malgun Gothic", 9, "bold"),
        )
        icon_label.pack(side="left", padx=(0, 8))

        def apply_icon() -> None:
            try:
                image = self.icon_cache.get(champion_id, 34)
                if icon_label.winfo_exists() and image:
                    icon_label.configure(image=image, text="", width=0)
            except tk.TclError:
                return

        icon = self.icon_cache.get(champion_id, 34, apply_icon)
        if icon:
            icon_label.configure(image=icon, text="", width=0)
        name = tk.Frame(outer, bg=COLORS["surface"])
        name.pack(side="left", fill="x", expand=True)
        title = tk.Frame(name, bg=COLORS["surface"])
        title.pack(fill="x")
        tk.Label(
            title, text=self._champion_text(champion_id), bg=COLORS["surface"],
            fg=COLORS["text"], font=("Malgun Gothic", 8, "bold"),
        ).pack(side="left")
        badge_color = POSITION_BADGE_COLORS.get(position, COLORS["muted"])
        tk.Label(
            title,
            text=(
                f"{POSITION_GLYPHS.get(position, '?')} "
                f"{self._position_text(position)}"
            ),
            bg=COLORS["chip"], fg=badge_color, padx=4, pady=1,
            font=("Segoe UI Symbol", 6, "bold"),
        ).pack(side="left", padx=(5, 0))
        tk.Label(
            name,
            text=(
                self._games_text(int(getattr(stat, "games", 0) or 0))
                + f" · KDA {getattr(stat, 'kda', None) or 0:.2f}"
            ),
            bg=COLORS["surface"], fg=COLORS["muted"], font=("Malgun Gothic", 7),
        ).pack(anchor="w")
        rate = getattr(stat, "win_rate", None)
        color = COLORS["green"] if rate is not None and rate >= 50 else COLORS["red"]
        tk.Label(
            outer, text=self._tr(_fmt_rate(rate)), bg=COLORS["surface"], fg=color,
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="right")

    @staticmethod
    def _participant_loadout_ids(participant: dict) -> tuple[tuple[int, ...], int, int]:
        spells = tuple(
            spell_id for spell_id in (
                int(participant.get("summoner1Id") or 0),
                int(participant.get("summoner2Id") or 0),
            ) if spell_id
        )
        styles = (participant.get("perks") or {}).get("styles") or []
        primary_rune = next(
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
            int(participant.get("perkSubStyle") or 0),
        )
        return spells, primary_rune, secondary_style

    def _loadout_assets(
        self,
        spell_ids: tuple[int, ...],
        primary_rune_id: int,
        secondary_style_id: int,
    ) -> list[tuple[str, int, str, str, str]]:
        assets: list[tuple[str, int, str, str, str]] = []
        version = self.registry.version
        for spell_id in spell_ids[:2]:
            name, filename = SUMMONER_SPELLS.get(
                int(spell_id), (f"스펠 #{spell_id}", "")
            )
            if self.ui_language == "en":
                name = SUMMONER_SPELL_NAMES_EN.get(
                    int(spell_id), f"Summoner spell {spell_id}",
                )
            url = (
                f"https://ddragon.leagueoflegends.com/cdn/{version}/img/spell/{filename}"
                if filename and version != "fallback" else ""
            )
            assets.append(("spell", int(spell_id), name, url, name))
        if primary_rune_id:
            option = self.rune_catalog.perk(primary_rune_id)
            name = self._rune_name_text(
                primary_rune_id, option.name if option else "",
            )
            url = option.icon_url if option else ""
            tooltip = (
                f"{name}\nRune ID {primary_rune_id}"
                if self.ui_language == "en"
                else self.rune_catalog.tooltip_text(primary_rune_id, name)
            )
            assets.append(("rune", primary_rune_id, name, url, tooltip))
        if secondary_style_id:
            style = self.rune_catalog.style(secondary_style_id)
            name = self._rune_style_text(
                secondary_style_id, style.name if style else "",
            )
            url = style.icon_url if style else ""
            assets.append((
                "rune-style", secondary_style_id, name, url,
                f"Secondary rune path · {name}"
                if self.ui_language == "en" else f"보조 룬 · {name}",
            ))
        return assets

    def _loadout_icon(
        self,
        parent: tk.Widget,
        asset: tuple[str, int, str, str, str],
        size: int,
        bg: str,
    ) -> tk.Label:
        kind, asset_id, name, url, tooltip_text = asset
        label = tk.Label(
            parent, text=name[:1] if name else "?", bg=COLORS["chip"],
            fg=COLORS["text"], width=max(2, size // 10), height=1,
            font=("Malgun Gothic", 6, "bold"), highlightthickness=1,
            highlightbackground=bg,
        )

        def apply() -> None:
            try:
                if not label.winfo_exists():
                    return
                image = self.build_icon_cache.get(f"history:{kind}:{asset_id}", url, size)
                if image:
                    label.configure(image=image, text="", width=0, height=0)
            except tk.TclError:
                return

        image = self.build_icon_cache.get(
            f"history:{kind}:{asset_id}", url, size, apply if url else None
        )
        if image:
            label.configure(image=image, text="", width=0, height=0)
        helper = _HoverTooltip(label, lambda value=tooltip_text: value)
        setattr(label, "_advisor_tooltip", helper)
        return label

    def _render_loadout_icons(
        self,
        parent: tk.Widget,
        spell_ids: tuple[int, ...],
        primary_rune_id: int,
        secondary_style_id: int,
        *,
        size: int = 20,
        bg: str | None = None,
    ) -> None:
        background = bg or str(parent.cget("bg"))
        assets = self._loadout_assets(
            spell_ids, primary_rune_id, secondary_style_id
        )
        if not assets:
            tk.Label(
                parent, text="스펠·룬 미기록", bg=background, fg=COLORS["muted"],
                font=("Malgun Gothic", 6),
            ).pack(anchor="w")
            return
        for index, asset in enumerate(assets):
            icon = self._loadout_icon(parent, asset, size, background)
            icon.grid(row=index // 2, column=index % 2, padx=1, pady=1)

    def _render_history_match(
        self,
        entry: MatchHistoryEntry,
        parent: tk.Widget | None = None,
        perspective_puuid: str = "",
    ) -> None:
        result_color, result_bg, result_badge_bg = history_result_style(entry.won)
        target = parent or self.history_matches_frame
        outer = tk.Frame(target, bg=result_color, padx=2, pady=2)
        outer.pack(fill="x", pady=4)
        card = tk.Frame(outer, bg=result_bg, padx=9, pady=7)
        card.pack(fill="x")
        summary = tk.Frame(card, bg=result_bg)
        summary.pack(fill="x")
        # Reserve the action first. The previous left-to-right fixed widths
        # could consume the whole row and push this button beyond the card.
        self._button(
            summary, "상세 보기",
            lambda match_id=entry.match_id, player_puuid=perspective_puuid: (
                self._open_match_detail(match_id, player_puuid)
            ),
            COLORS["purple"], width=9,
        ).pack(side="right", padx=(8, 0), anchor="n")

        loadout = tk.Frame(summary, bg=result_bg)
        loadout.pack(side="left", fill="y", padx=(0, 9))
        champion_label = tk.Label(
            loadout, text=self._champion_text(entry.champion_id)[:1],
            bg=COLORS["chip"], fg=COLORS["gold"], width=52,
            font=("Malgun Gothic", 13, "bold"), highlightthickness=1,
            highlightbackground=result_color,
        )
        champion_label.pack(side="left", padx=(0, 4))

        def apply_champion_icon() -> None:
            try:
                image = self.icon_cache.get(entry.champion_id, 52)
                if champion_label.winfo_exists() and image:
                    champion_label.configure(image=image, text="", width=0)
            except tk.TclError:
                return

        icon = self.icon_cache.get(entry.champion_id, 52, apply_champion_icon)
        if icon:
            champion_label.configure(image=icon, text="", width=0)
        loadout_icons = tk.Frame(loadout, bg=result_bg)
        loadout_icons.pack(side="left", anchor="n")
        self._render_loadout_icons(
            loadout_icons,
            entry.summoner_spell_ids,
            entry.primary_rune_id,
            entry.secondary_rune_style_id,
            size=20,
            bg=result_bg,
        )
        # LP and prediction are separate rows. Reserve enough height so the
        # second badge cannot be painted underneath the lineup strip.
        result = tk.Frame(summary, bg=result_bg, width=178, height=82)
        result.pack(side="left", fill="y")
        result.pack_propagate(False)
        result_heading = tk.Frame(result, bg=result_bg)
        result_heading.pack(fill="x")
        tk.Label(
            result_heading, text=self._tr("승리" if entry.won else "패배"),
            bg=result_color, fg="#07101b", padx=7, pady=2,
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="left")
        tk.Label(
            result_heading, text=self._champion_text(entry.champion_id),
            bg=result_bg, fg=result_color, padx=6,
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        tk.Label(
            result,
            text=f"{self._history_time(entry.game_creation)} · {entry.duration_seconds // 60}:{entry.duration_seconds % 60:02d}",
            bg=result_bg, fg=COLORS["muted"], font=("Malgun Gothic", 7),
        ).pack(anchor="w", pady=(4, 0))
        lp_badge = exact_lp_badge_text(entry)
        if lp_badge:
            lp_color = (
                COLORS["green"] if entry.lp_delta and entry.lp_delta > 0
                else COLORS["red"] if entry.lp_delta and entry.lp_delta < 0
                else COLORS["gold"]
            )
            tk.Label(
                result, text=lp_badge, bg=result_badge_bg, fg=lp_color,
                padx=5, pady=1, font=("Malgun Gothic", 6, "bold"),
            ).pack(anchor="w", pady=(4, 0))
        if entry.predicted_win_rate is not None and entry.predicted_win is not None:
            prediction_color = (
                COLORS["green"] if entry.prediction_correct is True
                else COLORS["red"] if entry.prediction_correct is False
                else COLORS["gold"]
            )
            accuracy = (
                self._tr("적중") if entry.prediction_correct is True
                else self._tr("빗나감") if entry.prediction_correct is False
                else self._tr("결과 대기")
            )
            tk.Label(
                result,
                text=self._text(
                    "history.result_prediction",
                    rate=entry.predicted_win_rate,
                    result=self._tr("승" if entry.predicted_win else "패"),
                    accuracy=accuracy,
                ),
                bg=result_badge_bg, fg=prediction_color,
                padx=5, pady=1, font=("Malgun Gothic", 6, "bold"),
            ).pack(anchor="w", pady=(4, 0))

        core = tk.Frame(summary, bg=result_bg, width=180)
        core.pack(side="left", fill="y")
        core.pack_propagate(False)
        tk.Label(
            core, text=f"KDA {entry.kda:.2f}  ·  {entry.kills}/{entry.deaths}/{entry.assists}",
            bg=result_bg, fg=COLORS["gold"], font=("Malgun Gothic", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            core,
            text=self._text(
                "history.cs_vision", cs=entry.cs,
                per_minute=entry.cs_per_minute, vision=entry.vision_score,
            ),
            bg=result_bg, fg=COLORS["muted"], font=("Malgun Gothic", 7),
        ).pack(anchor="w", pady=(2, 0))
        if entry.performance_badges:
            badge_row = tk.Frame(core, bg=result_bg)
            badge_row.pack(anchor="w", pady=(4, 0))
            self._render_history_performance_badges(
                badge_row, entry, result_badge_bg,
            )

        items = tk.Frame(summary, bg=result_bg, width=210)
        items.pack(side="left", fill="y", padx=(3, 8))
        items.pack_propagate(False)
        tk.Label(
            items, text=self._tr("아이템"), bg=result_bg, fg=COLORS["muted"],
            font=("Malgun Gothic", 6, "bold"),
        ).pack(anchor="w")
        item_row = tk.Frame(items, bg=result_bg)
        item_row.pack(anchor="w", pady=(2, 0))
        if not entry.items:
            tk.Label(
                item_row, text=self._tr("기록 없음"), bg=result_bg, fg=COLORS["muted"],
                font=("Malgun Gothic", 7),
            ).pack(side="left")
        for item_id in entry.items[:7]:
            item_label = tk.Label(
                item_row, text=str(item_id)[-2:],
                bg=COLORS["chip"], fg=COLORS["muted"], width=24,
                font=("Consolas", 6),
            )
            item_label.pack(side="left", padx=(0, 2))

            def apply_item_icon(
                label: tk.Label = item_label, value: int = item_id,
            ) -> None:
                try:
                    image = self.item_icon_cache.get(value, 24)
                    if label.winfo_exists() and image:
                        label.configure(image=image, text="", width=0)
                except tk.TclError:
                    return

            image = self.item_icon_cache.get(item_id, 24, apply_item_icon)
            if image:
                item_label.configure(image=image, text="", width=0)
            tooltip = _HoverTooltip(
                item_label,
                lambda value=item_id: self.item_icon_cache.tooltip_text(value),
            )
            setattr(item_label, "_advisor_tooltip", tooltip)

        # Put both complete teams on their own full-width rows. This prevents
        # the fixed summary columns from clipping players 4-5 on narrower
        # windows and makes the ten champion icons scannable at a glance.
        lineup = tk.Frame(card, bg=COLORS["panel_2"], padx=7, pady=5)
        lineup.pack(fill="x", pady=(7, 0))
        team_players = (
            entry.ally_players or tuple(
                (champion_id, self.registry.ko_name(champion_id))
                for champion_id in entry.ally_champions
            ),
            entry.enemy_players or tuple(
                (champion_id, self.registry.ko_name(champion_id))
                for champion_id in entry.enemy_champions
            ),
        )
        for team_index, players in enumerate(team_players):
            team = tk.Frame(lineup, bg=COLORS["panel_2"])
            team.pack(fill="x", pady=(0 if team_index == 0 else 4, 0))
            tk.Label(
                team, text=self._tr("아군" if team_index == 0 else "적군"),
                bg=COLORS["panel_2"],
                fg=COLORS["blue"] if team_index == 0 else COLORS["red"],
                font=("Malgun Gothic", 7, "bold"), anchor="w", width=5,
            ).pack(side="left", padx=(0, 7))
            player_grid = tk.Frame(team, bg=COLORS["panel_2"])
            player_grid.pack(side="left", fill="x", expand=True)
            for column in range(5):
                player_grid.grid_columnconfigure(
                    column, weight=1, uniform=f"history_team_{team_index}"
                )
            for column, (champion_id, riot_id) in enumerate(players[:5]):
                player_row = tk.Frame(player_grid, bg=COLORS["panel_2"])
                player_row.grid(
                    row=0, column=column, sticky="ew", padx=(0, 4), pady=1
                )
                player_icon = tk.Label(
                    player_row, text="?", bg=COLORS["chip"],
                    fg=COLORS["muted"], width=18,
                )
                player_icon.pack(side="left", padx=(0, 3))

                def apply_player_icon(
                    label: tk.Label = player_icon, value: str = champion_id,
                ) -> None:
                    try:
                        image = self.icon_cache.get(value, 18)
                        if label.winfo_exists() and image:
                            label.configure(image=image, text="", width=0)
                    except tk.TclError:
                        return

                image = self.icon_cache.get(champion_id, 18, apply_player_icon)
                if image:
                    player_icon.configure(image=image, text="", width=0)
                display_name = riot_id if len(riot_id) <= 15 else riot_id[:14] + "…"
                name_label = tk.Label(
                    player_row, text=display_name, bg=COLORS["panel_2"],
                    fg=COLORS["blue"], font=("Malgun Gothic", 7, "bold"),
                    anchor="w", cursor="hand2",
                )
                name_label.pack(side="left", fill="x", expand=True)
                name_label.bind(
                    "<Button-1>",
                    lambda _event, value=riot_id: self._open_player_history_tab(value),
                    add="+",
                )
                helper = _HoverTooltip(name_label, lambda value=riot_id: value)
                setattr(name_label, "_advisor_tooltip", helper)

    def _render_history_performance_badges(
        self, parent: tk.Widget, entry: MatchHistoryEntry, background: str,
    ) -> None:
        badge_values = {
            "CC": (
                "history.badge.cc", COLORS["purple"],
                self._text(
                    "history.badge.cc_tip", seconds=entry.time_ccing_others,
                ),
            ),
            "VISION": (
                "history.badge.vision", COLORS["blue"],
                self._text(
                    "history.badge.vision_tip", score=entry.vision_score,
                    wards=entry.wards_placed, controls=entry.control_wards_placed,
                ),
            ),
            "TANKING": (
                "history.badge.tanking", COLORS["green"],
                self._text(
                    "history.badge.tanking_tip", taken=entry.damage_taken,
                    mitigated=entry.damage_self_mitigated,
                ),
            ),
            "DAMAGE": (
                "history.badge.damage", COLORS["red"],
                self._text(
                    "history.badge.damage_tip", damage=entry.damage_to_champions,
                ),
            ),
            "TEAMPLAY": (
                "history.badge.teamplay", COLORS["gold"],
                self._text(
                    "history.badge.teamplay_tip",
                    rate=entry.kill_participation or 0.0,
                ),
            ),
            "PERFECT_KDA": (
                "history.badge.perfect_kda", COLORS["gold"],
                self._text(
                    "history.badge.perfect_kda_tip", kills=entry.kills,
                    assists=entry.assists,
                ),
            ),
            "KILL_CARRY": (
                "history.badge.kill_carry", COLORS["red"],
                self._text("history.badge.kill_carry_tip", kills=entry.kills),
            ),
            "ASSIST_MASTER": (
                "history.badge.assist_master", COLORS["purple"],
                self._text(
                    "history.badge.assist_master_tip", assists=entry.assists,
                ),
            ),
            "PROTECTOR": (
                "history.badge.protector", COLORS["green"],
                self._text(
                    "history.badge.protector_tip",
                    healing=entry.healing_on_teammates,
                    shielding=entry.shielding_on_teammates,
                ),
            ),
            "OBJECTIVE": (
                "history.badge.objective", COLORS["blue"],
                self._text(
                    "history.badge.objective_tip",
                    damage=entry.damage_to_objectives,
                ),
            ),
            "SIEGE": (
                "history.badge.siege", COLORS["orange"],
                self._text(
                    "history.badge.siege_tip", damage=entry.damage_to_turrets,
                    kills=entry.turret_kills,
                ),
            ),
            "WARD_CLEAR": (
                "history.badge.ward_clear", COLORS["blue"],
                self._text(
                    "history.badge.ward_clear_tip", wards=entry.wards_killed,
                ),
            ),
            "SURVIVOR": (
                "history.badge.survivor", COLORS["green"],
                self._text(
                    "history.badge.survivor_tip", deaths=entry.deaths,
                    minutes=entry.duration_seconds // 60,
                ),
            ),
            "KILLING_SPREE": (
                "history.badge.killing_spree", COLORS["red"],
                self._text(
                    "history.badge.killing_spree_tip",
                    spree=entry.largest_killing_spree,
                    multi=entry.largest_multi_kill,
                ),
            ),
            "FIRST_BLOOD": (
                "history.badge.first_blood", COLORS["red"],
                self._text("history.badge.first_blood_tip"),
            ),
            "OBJECTIVE_STEAL": (
                "history.badge.objective_steal", COLORS["gold"],
                self._text(
                    "history.badge.objective_steal_tip",
                    count=entry.objectives_stolen,
                ),
            ),
            "FARM": (
                "history.badge.farm", COLORS["green"],
                self._text(
                    "history.badge.farm_tip", cs=entry.cs,
                    per_minute=entry.cs_per_minute,
                ),
            ),
        }
        for code in entry.performance_badges[:3]:
            definition = badge_values.get(code)
            if not definition:
                continue
            text_key, color, detail = definition
            label = tk.Label(
                parent, text=self._text(text_key), bg=background, fg=color,
                padx=5, pady=1, font=("Malgun Gothic", 6, "bold"),
                cursor="hand2",
            )
            label.pack(side="left", padx=(0, 3))
            tooltip = _HoverTooltip(label, lambda value=detail: value)
            setattr(label, "_advisor_tooltip", tooltip)

    def _history_time(self, game_creation: int) -> str:
        if not game_creation:
            return self._text("time.unknown")
        played = datetime.fromtimestamp(game_creation / 1000)
        elapsed = datetime.now() - played
        if elapsed < timedelta(hours=1):
            return self._text(
                "time.minutes_ago",
                value=max(int(elapsed.total_seconds() // 60), 1),
            )
        if elapsed < timedelta(days=1):
            return self._text(
                "time.hours_ago", value=int(elapsed.total_seconds() // 3600),
            )
        if elapsed < timedelta(days=7):
            return self._text("time.days_ago", value=elapsed.days)
        return played.strftime("%m.%d %H:%M")

    def _show_more_history(self) -> None:
        overview = self.history_overview
        if not overview:
            return
        entries = [
            entry for entry in overview.entries
            if self._history_result_filter == "ALL"
            or (self._history_result_filter == "WIN" and entry.won)
            or (self._history_result_filter == "LOSS" and not entry.won)
        ]
        old_count = self._history_visible_count
        self._history_visible_count += 10
        for entry in entries[old_count:self._history_visible_count]:
            self._render_history_match(entry)
        remaining = len(entries) - self._history_visible_count
        self.history_more_button.configure(
            state="normal" if remaining > 0 else "disabled",
            text=(
                self._text("history.more", remaining=remaining)
                if remaining > 0 else self._text("history.all_shown")
            ),
        )
        self._history_matches_signature = repr((
            self._history_asset_revision,
            tuple(overview.entries),
            self._history_result_filter,
            self._history_visible_count,
            None,
        ))

    def _open_match_detail(
        self, match_id: str, perspective_puuid: str = "",
    ) -> None:
        match = self.storage.load_match(match_id)
        puuid = str(perspective_puuid or "").strip() or self._history_puuid()
        if not match or not puuid:
            messagebox.showwarning("경기 상세", "로컬 경기 데이터를 찾지 못했습니다.", parent=self.root)
            return
        info = match.get("info") or {}
        participants = info.get("participants") or []
        mine = next((row for row in participants if row.get("puuid") == puuid), None)
        if not mine:
            messagebox.showwarning(
                "경기 상세", "선택한 플레이어의 참가자 기록을 찾지 못했습니다.",
                parent=self.root,
            )
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"경기 상세 · {match_id}")
        dialog.configure(bg=COLORS["bg"])
        dialog.geometry("1320x860")
        dialog.minsize(1050, 700)
        dialog.transient(self.root)

        canvas = tk.Canvas(dialog, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            dialog, orient="vertical", command=canvas.yview,
            style="Advisor.Vertical.TScrollbar",
        )
        content = tk.Frame(canvas, bg=COLORS["bg"])
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        setattr(dialog, "_advisor_scroll_canvas", canvas)
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(content_window, width=e.width))

        won = bool(mine.get("win"))
        result_color = COLORS["green"] if won else COLORS["red"]
        header = tk.Frame(content, bg=COLORS["bg"], padx=22, pady=16)
        header.pack(fill="x")
        icon = self.icon_cache.get(str(mine.get("championName") or "Unknown"), 64)
        tk.Label(
            header, image=icon or "", text="" if icon else "?", bg=COLORS["chip"],
            fg=COLORS["gold"], width=64 if not icon else 0,
        ).pack(side="left", padx=(0, 12))
        title = tk.Frame(header, bg=COLORS["bg"])
        title.pack(side="left", fill="x", expand=True)
        tk.Label(
            title,
            text=f"{'승리' if won else '패배'} · {self.registry.ko_name(str(mine.get('championName') or ''))}",
            bg=COLORS["bg"], fg=result_color, font=("Malgun Gothic", 17, "bold"),
        ).pack(anchor="w")
        duration = int(info.get("gameDuration") or mine.get("timePlayed") or 0)
        creation = int(info.get("gameCreation") or 0)
        tk.Label(
            title,
            text=f"솔로랭크 · {duration // 60}:{duration % 60:02d} · {self._history_time(creation)} · {match_id}",
            bg=COLORS["bg"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
        ).pack(anchor="w", pady=(3, 0))
        prediction = self.storage.load_game_predictions([match_id]).get(match_id)
        if prediction:
            prediction_color = (
                COLORS["green"] if prediction.correct is True
                else COLORS["red"] if prediction.correct is False
                else COLORS["gold"]
            )
            tk.Label(
                title,
                text=(
                    f"시작 전 예상 {prediction.win_probability:.1f}% · "
                    f"{'승리' if prediction.predicted_win else '패배'} 예상 · "
                    f"{'적중' if prediction.correct is True else '빗나감' if prediction.correct is False else '결과 대기'} · "
                    f"신뢰도 {prediction.confidence}"
                ),
                bg=COLORS["bg"], fg=prediction_color,
                font=("Malgun Gothic", 8, "bold"),
            ).pack(anchor="w", pady=(4, 0))
        self._button(header, "닫기", dialog.destroy, COLORS["muted"]).pack(side="right")

        team_id = mine.get("teamId")
        team_kills = sum(
            int(row.get("kills") or 0) for row in participants if row.get("teamId") == team_id
        )
        kills = int(mine.get("kills") or 0)
        deaths = int(mine.get("deaths") or 0)
        assists = int(mine.get("assists") or 0)
        kda = (kills + assists) / max(deaths, 1)
        participation = (kills + assists) / team_kills * 100 if team_kills else None
        cs = int(mine.get("totalMinionsKilled") or 0) + int(mine.get("neutralMinionsKilled") or 0)
        metrics = tk.Frame(content, bg=COLORS["bg"])
        metrics.pack(fill="x", padx=22, pady=(0, 12))
        for index, (label, value, detail, accent) in enumerate((
            ("KDA", f"{kda:.2f}", f"{kills}/{deaths}/{assists}", COLORS["gold"]),
            ("킬 관여", _fmt_rate(participation), f"팀 {team_kills}킬", COLORS["green"]),
            ("챔피언 피해", f"{int(mine.get('totalDamageDealtToChampions') or 0):,}", "가한 피해량", COLORS["red"]),
            ("시야 점수", f"{int(mine.get('visionScore') or 0)}", f"와드 {int(mine.get('wardsPlaced') or 0)}개", COLORS["blue"]),
            ("CS/분", f"{cs / max(duration / 60, 1):.1f}", f"총 {cs} CS", COLORS["purple"]),
        )):
            outer, value_label, detail_label = self._mini_metric(metrics, label, accent)
            value_label.configure(text=value)
            detail_label.configure(text=detail)
            outer.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 4, 4))

        owner_puuid = self._history_puuid()
        detail_panel = self._panel(
            content,
            "내 상세 지표" if puuid == owner_puuid else "선택 플레이어 상세 지표",
            COLORS["purple"],
        )
        detail_columns = tk.Frame(detail_panel, bg=COLORS["panel"])
        detail_columns.pack(fill="x")
        combat = tk.Frame(detail_columns, bg=COLORS["panel_2"], padx=12, pady=10)
        vision = tk.Frame(detail_columns, bg=COLORS["panel_2"], padx=12, pady=10)
        combat.pack(side="left", fill="both", expand=True, padx=(0, 6))
        vision.pack(side="left", fill="both", expand=True, padx=(6, 0))
        tk.Label(
            combat, text="전투", bg=COLORS["panel_2"], fg=COLORS["red"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w", pady=(0, 5))
        for label, value in (
            ("가한 챔피언 피해", f"{int(mine.get('totalDamageDealtToChampions') or 0):,}"),
            ("받은 피해", f"{int(mine.get('totalDamageTaken') or 0):,}"),
            ("피해 감소", f"{int(mine.get('damageSelfMitigated') or 0):,}"),
            ("골드 획득", f"{int(mine.get('goldEarned') or 0):,}"),
            ("적 CC 시간", f"{int(mine.get('timeCCingOthers') or 0)}초"),
        ):
            self._detail_stat_row(combat, label, value)
        tk.Label(
            vision, text="시야·지원", bg=COLORS["panel_2"], fg=COLORS["blue"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w", pady=(0, 5))
        for label, value in (
            ("시야 점수", f"{int(mine.get('visionScore') or 0)}"),
            ("와드 설치 / 제거", f"{int(mine.get('wardsPlaced') or 0)} / {int(mine.get('wardsKilled') or 0)}"),
            ("제어 와드", f"{int(mine.get('detectorWardsPlaced') or 0)}"),
            ("아군 치유", f"{int(mine.get('totalHealsOnTeammates') or 0):,}"),
            ("아군 보호막", f"{int(mine.get('totalDamageShieldedOnTeammates') or 0):,}"),
        ):
            self._detail_stat_row(vision, label, value)

        performance_ranks = participant_performance_ranks(participants)
        for current_team in (team_id, next((row.get("teamId") for row in participants if row.get("teamId") != team_id), None)):
            if current_team is None:
                continue
            team_rows = [row for row in participants if row.get("teamId") == current_team]
            self._render_detail_team(
                content,
                team_rows,
                current_team == team_id,
                info,
                performance_ranks,
                perspective_puuid=puuid,
            )

    def _detail_stat_row(self, parent: tk.Widget, label: str, value: str) -> None:
        row = tk.Frame(parent, bg=COLORS["surface"], padx=9, pady=6)
        row.pack(fill="x", pady=2)
        tk.Label(
            row, text=label, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        ).pack(side="left")
        tk.Label(
            row, text=value, bg=COLORS["surface"], fg=COLORS["text"],
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="right")

    def _detail_participant_rank(self, participant: dict) -> tuple[str, str]:
        game_name = str(
            participant.get("riotIdGameName")
            or participant.get("summonerName") or ""
        ).strip()
        tag_line = str(
            participant.get("riotIdTagline")
            or participant.get("riotIdTagLine") or ""
        ).strip()
        riot_id = f"{game_name}#{tag_line}" if tag_line else game_name
        profile = self.player_profiles.get(riot_id)
        if profile:
            tier = str(profile.tier or "UNRANKED").upper()
            if tier == "UNRANKED":
                return "언랭크", COLORS["muted"]
            return (
                f"{tier} {profile.rank}\n{profile.league_points}LP",
                RANK_COLORS.get(tier, COLORS["muted"]),
            )
        opgg_profile = (
            self.storage.load_opgg_player_profile_any_age(riot_id) if riot_id else None
        )
        if opgg_profile:
            tier = str(opgg_profile.tier or "UNRANKED").upper()
            if tier == "UNRANKED":
                return "언랭크", COLORS["muted"]
            return (
                f"{tier} {opgg_profile.division}\n{opgg_profile.league_points}LP",
                RANK_COLORS.get(tier, COLORS["muted"]),
            )
        cached = self.storage.load_live_profile_any_age(riot_id) if riot_id else None
        if not cached:
            return "미확인", COLORS["muted"]
        _puuid, payload, _updated_at = cached
        entry = payload.get("solo_entry") or {}
        if not entry:
            return (
                ("언랭크", COLORS["muted"])
                if payload.get("rank_checked") or "solo_entry" in payload
                else ("미확인", COLORS["muted"])
            )
        tier = str(entry.get("tier") or "UNRANKED").upper()
        if tier == "UNRANKED":
            return "언랭크", COLORS["muted"]
        return (
            f"{tier} {entry.get('rank') or ''}\n{int(entry.get('leaguePoints') or 0)}LP",
            RANK_COLORS.get(tier, COLORS["muted"]),
        )

    def _render_detail_team(
        self,
        parent: tk.Widget,
        participants: list[dict],
        ally: bool,
        info: dict,
        performance_ranks: dict[str, int],
        perspective_puuid: str = "",
    ) -> None:
        accent = COLORS["blue"] if ally else COLORS["red"]
        title = "아군" if ally else "적군"
        team_id = participants[0].get("teamId") if participants else 0
        kills = sum(int(row.get("kills") or 0) for row in participants)
        deaths = sum(int(row.get("deaths") or 0) for row in participants)
        assists = sum(int(row.get("assists") or 0) for row in participants)
        gold = sum(int(row.get("goldEarned") or 0) for row in participants)
        team_payload = next(
            (row for row in (info.get("teams") or []) if row.get("teamId") == team_id), {}
        )
        objective_counts = team_objective_counts(team_payload)
        panel = self._panel(
            parent,
            f"{title} · {kills}/{deaths}/{assists} · {gold:,}골드 · "
            f"공허 유충 {objective_counts['void_grubs']} / "
            f"전령 {objective_counts['rift_heralds']} / "
            f"용 {objective_counts['dragons']} / 바론 {objective_counts['barons']} / "
            f"타워 {objective_counts['towers']}",
            accent,
        )
        header = tk.Frame(panel, bg=COLORS["chip"], padx=7, pady=5)
        header.pack(fill="x", pady=(0, 3))
        for text_value, width in (
            ("플레이어", 25), ("활약", 7), ("스펠·룬", 13),
            ("솔로랭크", 13), ("KDA", 12), ("CS", 7),
            ("시야", 7), ("챔피언 피해", 16), ("아이템", 25),
        ):
            tk.Label(
                header, text=text_value, width=width, bg=COLORS["chip"],
                fg=COLORS["muted"], anchor="w", font=("Malgun Gothic", 7, "bold"),
            ).pack(side="left")
        tk.Label(
            header, text="활약 순위는 Riot 경기 지표 기반 앱 계산",
            bg=COLORS["chip"], fg=COLORS["orange"],
            font=("Malgun Gothic", 6),
        ).pack(side="right")
        position_order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4, "SUPPORT": 4}
        rows = sorted(
            participants,
            key=lambda row: position_order.get(str(row.get("teamPosition") or "").upper(), 9),
        )
        max_damage = max((int(row.get("totalDamageDealtToChampions") or 0) for row in rows), default=1)
        owner_puuid = self._history_puuid()
        my_puuid = str(perspective_puuid or "").strip() or owner_puuid
        perspective_suffix = " · 나" if my_puuid == owner_puuid else " · 선택"
        for row_index, participant in enumerate(rows):
            is_me = participant.get("puuid") == my_puuid
            row_bg = "#19263b" if is_me else COLORS["surface"]
            row = tk.Frame(
                panel, bg=row_bg, padx=7, pady=6,
                highlightthickness=1 if is_me else 0,
                highlightbackground=COLORS["gold"],
            )
            row.pack(fill="x", pady=1)
            identity = tk.Frame(row, bg=row_bg, width=210)
            identity.pack(side="left", fill="y")
            identity.pack_propagate(False)
            champion_id = str(participant.get("championName") or "Unknown")
            icon = self.icon_cache.get(champion_id, 34)
            tk.Label(
                identity, image=icon or "", text="" if icon else "?", bg=COLORS["chip"],
                fg=COLORS["muted"], width=34 if not icon else 0,
            ).pack(side="left", padx=(0, 7))
            names = tk.Frame(identity, bg=row_bg)
            names.pack(side="left", fill="x")
            riot_name = str(participant.get("riotIdGameName") or participant.get("summonerName") or "Unknown")
            tag = str(participant.get("riotIdTagline") or participant.get("riotIdTagLine") or "")
            tk.Label(
                names,
                text=(
                    f"{riot_name}{'#' + tag if tag else ''}"
                    f"{perspective_suffix if is_me else ''}"
                ),
                bg=row_bg, fg=COLORS["gold"] if is_me else COLORS["text"],
                font=("Malgun Gothic", 8, "bold"),
            ).pack(anchor="w")
            tk.Label(
                names, text=f"{self.registry.ko_name(champion_id)} · Lv.{int(participant.get('champLevel') or 0)}",
                bg=row_bg, fg=COLORS["muted"], font=("Malgun Gothic", 7),
            ).pack(anchor="w")
            performance_rank = performance_ranks.get(
                participant_row_key(participant, row_index), 0
            )
            rank_badge_color = (
                COLORS["gold"] if performance_rank == 1
                else COLORS["green"] if 1 < performance_rank <= 3
                else COLORS["blue"] if 3 < performance_rank <= 5
                else COLORS["muted"]
            )
            tk.Label(
                row, text=f"{performance_rank}등" if performance_rank else "--",
                width=6, bg=row_bg, fg=rank_badge_color,
                font=("Malgun Gothic", 8, "bold"), anchor="w",
            ).pack(side="left")
            loadout_frame = tk.Frame(row, bg=row_bg, width=105)
            loadout_frame.pack(side="left", fill="y")
            loadout_frame.pack_propagate(False)
            spells, primary_rune, secondary_style = self._participant_loadout_ids(
                participant
            )
            loadout_grid = tk.Frame(loadout_frame, bg=row_bg)
            loadout_grid.pack(anchor="w")
            self._render_loadout_icons(
                loadout_grid, spells, primary_rune, secondary_style,
                size=22, bg=row_bg,
            )
            rank_text, rank_color = self._detail_participant_rank(participant)
            tk.Label(
                row, text=rank_text, width=11,
                bg=row_bg, fg=rank_color,
                font=("Malgun Gothic", 7, "bold"), justify="left", anchor="w",
            ).pack(side="left")
            kills = int(participant.get("kills") or 0)
            deaths = int(participant.get("deaths") or 0)
            assists = int(participant.get("assists") or 0)
            kda = (kills + assists) / max(deaths, 1)
            tk.Label(
                row, text=f"{kills}/{deaths}/{assists}\n{kda:.1f}", width=10,
                bg=row_bg, fg=COLORS["gold"] if kda >= 4 else COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), justify="left", anchor="w",
            ).pack(side="left")
            cs = int(participant.get("totalMinionsKilled") or 0) + int(participant.get("neutralMinionsKilled") or 0)
            tk.Label(
                row, text=str(cs), width=7, bg=row_bg, fg=COLORS["text"],
                font=("Malgun Gothic", 8), anchor="w",
            ).pack(side="left")
            tk.Label(
                row, text=str(int(participant.get("visionScore") or 0)), width=7,
                bg=row_bg, fg=COLORS["blue"], font=("Malgun Gothic", 8), anchor="w",
            ).pack(side="left")
            damage = int(participant.get("totalDamageDealtToChampions") or 0)
            damage_frame = tk.Frame(row, bg=row_bg, width=135)
            damage_frame.pack(side="left", fill="y", padx=(0, 8))
            damage_frame.pack_propagate(False)
            tk.Label(
                damage_frame, text=f"{damage:,}", bg=row_bg, fg=COLORS["text"],
                font=("Malgun Gothic", 7, "bold"),
            ).pack(anchor="w")
            bar = tk.Frame(damage_frame, bg="#2b3342", height=4, width=115)
            bar.pack(anchor="w", pady=(2, 0))
            tk.Frame(bar, bg=accent).place(
                x=0, y=0, relwidth=damage / max(max_damage, 1), relheight=1
            )
            item_frame = tk.Frame(row, bg=row_bg)
            item_frame.pack(side="left", fill="x", expand=True)
            item_ids = [int(participant.get(f"item{index}") or 0) for index in range(7)]
            for item_id in (value for value in item_ids if value):
                item_label = tk.Label(
                    item_frame, text=str(item_id)[-2:], bg=COLORS["chip"],
                    fg=COLORS["muted"], width=3,
                    font=("Consolas", 6),
                )
                item_label.pack(side="left", padx=(0, 2))
                item_icon = self.item_icon_cache.get(
                    item_id, 24,
                    lambda label=item_label, value=item_id: self._apply_detail_item_icon(label, value),
                )
                if item_icon:
                    item_label.configure(image=item_icon, text="", width=0)
                tooltip = _HoverTooltip(
                    item_label,
                    lambda value=item_id: self.item_icon_cache.tooltip_text(value),
                )
                setattr(item_label, "_advisor_tooltip", tooltip)

    def _apply_detail_item_icon(self, label: tk.Label, item_id: int) -> None:
        try:
            if not label.winfo_exists():
                return
            image = self.item_icon_cache.get(item_id, 24)
            if image:
                label.configure(image=image, text="", width=0)
        except tk.TclError:
            return

    def _stat_block(self, parent: tk.Widget, title: str, color: str, body: str) -> None:
        frame = tk.Frame(parent, bg="#101827", padx=9, pady=7)
        frame.pack(fill="x", pady=(0, 6))
        tk.Label(frame, text=title, bg="#101827", fg=color,
                 font=("Malgun Gothic", 9, "bold")).pack(anchor="w")
        tk.Label(frame, text=body, bg="#101827", fg=COLORS["text"], justify="left",
                 font=("Malgun Gothic", 9)).pack(anchor="w", pady=(2, 0))

    def _paragraph(self, parent: tk.Widget, title: str, body: str, color: str | None = None) -> None:
        tk.Label(parent, text=f"{title}  {body}", bg=COLORS["panel_2"],
                 fg=color or COLORS["text"], justify="left", anchor="w",
                 wraplength=390, font=("Malgun Gothic", 9)).pack(fill="x", pady=(3, 0))

    def _counter_for(self, champion_id: str) -> OpggCounter | None:
        snapshots = [
            snapshot for snapshot in (self.opgg_snapshot, self.opgg_meta_snapshot)
            if snapshot is not None
        ]
        return next(
            (
                item for snapshot in snapshots
                for item in snapshot.counters + snapshot.weak_picks
                if item.champion_id == champion_id
            ),
            None,
        )

    def _synergy_for(self, champion_id: str) -> OpggSynergyStat | None:
        adc = allied_adc_member(self.draft)
        snapshot = self.opgg_synergy_snapshot
        if (
            self.draft.my_role != "SUPPORT" or not adc or not snapshot
            or snapshot.ally_champion_id != adc.champion_id
        ):
            return None
        return snapshot.synergy_for(champion_id)

    def _select_enemy_support(self, champion_id: str) -> None:
        self._manual_enemy_support = champion_id
        self.draft.selected_enemy_support_id = champion_id
        self.draft.selected_enemy_support_name_ko = self.registry.ko_name(champion_id)
        self.draft.selected_enemy_support_source = "MANUAL_ENEMY_SUPPORT"
        self.draft.refresh_snapshot_id()
        cached = self.storage.load_opgg_snapshot(champion_id, self.draft.my_role)
        self.opgg_snapshot = cached
        self._render_selection()
        # Manual enemy changes must refresh the recommendation-card matchup
        # data even before the local player hovers or locks a champion.
        self._sync_selected_matchup()
        self._sync_hover_matchup()

    def _select_unknown_enemy_support(self) -> None:
        self._manual_enemy_support = MANUAL_UNKNOWN_SUPPORT
        self.draft.selected_enemy_support_id = None
        self.draft.selected_enemy_support_name_ko = "모르겠음"
        self.draft.selected_enemy_support_source = "MANUAL_UNKNOWN"
        self.draft.refresh_snapshot_id()
        self.opgg_meta_snapshot = self.storage.load_opgg_snapshot(None, self.draft.my_role)
        self.opgg_snapshot = self.opgg_meta_snapshot
        self._render_selection()
        self._sync_hover_matchup()

    def _auto_select_enemy_support(self, draft: DraftSnapshot) -> None:
        if self._manual_enemy_support == MANUAL_UNKNOWN_SUPPORT:
            draft.selected_enemy_support_id = None
            draft.selected_enemy_support_name_ko = "모르겠음"
            draft.selected_enemy_support_source = "MANUAL_UNKNOWN"
            return
        if self._manual_enemy_support:
            draft.selected_enemy_support_id = self._manual_enemy_support
            draft.selected_enemy_support_name_ko = self.registry.ko_name(self._manual_enemy_support)
            draft.selected_enemy_support_source = "MANUAL_ENEMY_SUPPORT"
            return
        role_match = next(
            (
                member for member in draft.enemy_locked
                if member.champion_id and member.role == draft.my_role
            ),
            None,
        )
        if self._support_catalog_ids is None:
            catalog = self.storage.load_opgg_position_catalog(
                "SUPPORT", max_age=None,
            )
            self._support_catalog_ids = set(catalog[1] if catalog else ())
        candidates = (
            [
                member for member in draft.enemy_locked
                if member.champion_id and (
                    member.champion_id in self._support_catalog_ids
                    or self.registry.support_score(member.champion_id)
                )
            ]
            if draft.my_role == "SUPPORT" else []
        )
        chosen = role_match or (
            max(
                candidates,
                key=lambda member: (
                    100 if member.champion_id in self._support_catalog_ids else 0,
                    self.registry.support_score(member.champion_id),
                    -(member.pick_order or 99),
                ),
            )
            if candidates else None
        )
        if chosen:
            draft.selected_enemy_support_id = chosen.champion_id
            draft.selected_enemy_support_name_ko = chosen.champion_name_ko
            draft.selected_enemy_support_source = "AUTO_ENEMY_SUPPORT"
        else:
            draft.selected_enemy_support_id = None
            draft.selected_enemy_support_name_ko = ""
            draft.selected_enemy_support_source = "UNKNOWN"

    def _copy_memory_prompt(self) -> None:
        prompt = build_memory_prompt()
        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        self.root.update_idletasks()
        self.storage.set_setting("prompt_memory_version", MEMORY_PROMPT_VERSION)
        self.exchange_status.configure(
            text="1회용 규칙 복사 완료 · 사용할 ChatGPT 채팅에 한 번만 붙여넣으세요",
            fg=COLORS["gold"],
        )
        self._render_prompt_summary()

    def _copy_prompt(self) -> None:
        prompt = build_prompt(
            self.draft, self.opgg_snapshot, self.opgg_meta_snapshot,
            self.opgg_synergy_snapshot, self._local_synergy_stats_for_prompt(),
            meta_limit=self._data_preference("opgg_meta_display_count"),
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        self.root.update_idletasks()
        self._prompt_copied_snapshot_id = self.draft.snapshot_id
        self.exchange_status.configure(
            text=f"짧은 질문 복사 완료 · {self.draft.snapshot_id}", fg=COLORS["purple"]
        )
        self._render_prompt_summary()

    def _request_codex_recommendations(self) -> None:
        if getattr(self, "demo", False):
            messagebox.showinfo(
                "데모 모드",
                "데모에서는 Codex CLI로 내용을 전송하지 않습니다.",
                parent=self.root,
            )
            return
        if not self.codex_recommendations_enabled:
            messagebox.showinfo(
                "픽 추천 꺼짐",
                "Riot 설정에서 ‘픽 추천 사용 (Codex CLI)’을 허용한 뒤 이용하세요.",
                parent=self.root,
            )
            return
        if self._codex_cli_running:
            return
        thread_id = self.storage.get_setting("codex_thread_id").strip()
        if not thread_id:
            self.exchange_status.configure(
                text="Riot 설정에서 ‘1회 규칙 보내기’를 먼저 누르세요.",
                fg=COLORS["orange"],
            )
            self._open_settings()
            return
        try:
            client = self._ensure_codex_cli_client()
        except CodexCliError as exc:
            self.exchange_status.configure(text=str(exc), fg=COLORS["red"])
            messagebox.showerror("Codex CLI", str(exc), parent=self.root)
            return
        prompt = build_prompt(
            self.draft, self.opgg_snapshot, self.opgg_meta_snapshot,
            self.opgg_synergy_snapshot, self._local_synergy_stats_for_prompt(),
            meta_limit=self._data_preference("opgg_meta_display_count"),
        )
        # A CLI turn takes roughly tens of seconds.  Keep the exact draft that
        # produced the prompt so a valid answer is not discarded merely
        # because League advanced to the next pick while Codex was replying.
        requested_draft = deepcopy(self.draft)
        self._codex_cli_running = True
        self._codex_cli_error = ""
        self._recommendation_apply_error = ""
        self.codex_recommend_button.configure(state="disabled", text="Codex 분석 중…")
        self.codex_cli_status_label.configure(
            text="Luna/none · 규칙+현재 드래프트만 빠르게 분석 중…",
            fg=COLORS["blue"],
        )
        self.exchange_status.configure(
            text="Codex CLI 요청 중 · 다른 탭과 롤 감지는 계속 동작합니다.",
            fg=COLORS["blue"],
        )
        self._selection_panel_signatures.pop("prompt", None)

        def success(turn: CodexTurn) -> None:
            self._codex_cli_running = False
            self.storage.set_setting("codex_thread_id", turn.thread_id)
            if not self.codex_recommendations_enabled:
                return
            self.response_text.delete("1.0", "end")
            self.response_text.insert("1.0", turn.message)
            applied = self._apply_recommendation_text(
                turn.message,
                show_dialog=False,
                draft_context=requested_draft,
                render_summary=False,
            )
            if applied:
                changed = self._recommendations_stale()
                self.exchange_status.configure(
                    text=(
                        f"분석 당시 추천 3개 표시 · {turn.model} · 실행 시 최신 세션 재확인"
                        if changed else f"CLI 추천 3개 적용 완료 · {turn.model}"
                    ),
                    fg=COLORS["orange"] if changed else COLORS["green"],
                )
            else:
                self._codex_cli_error = (
                    self._recommendation_apply_error
                    or "Codex 답변을 추천 카드로 변환하지 못했습니다."
                )
            self._selection_panel_signatures.pop("prompt", None)
            self._render_prompt_summary()

        def error(exc: Exception) -> None:
            self._codex_cli_running = False
            self._codex_cli_error = str(exc)
            self.exchange_status.configure(text=str(exc), fg=COLORS["red"])
            self._selection_panel_signatures.pop("prompt", None)
            self._render_prompt_summary()
            messagebox.showerror("Codex CLI 추천 실패", str(exc), parent=self.root)

        self._background(
            lambda: client.recommend(thread_id, prompt), success, error,
        )

    def _show_prompt_preview(self) -> None:
        prompt = build_prompt(
            self.draft, self.opgg_snapshot, self.opgg_meta_snapshot,
            self.opgg_synergy_snapshot, self._local_synergy_stats_for_prompt(),
            meta_limit=self._data_preference("opgg_meta_display_count"),
        )
        dialog = tk.Toplevel(self.root)
        dialog.title("ChatGPT에 보낼 짧은 질문 미리보기")
        dialog.configure(bg=COLORS["panel"])
        dialog.geometry("1000x720")
        dialog.transient(self.root)
        header = tk.Frame(dialog, bg=COLORS["panel"], padx=18, pady=14)
        header.pack(fill="x")
        tk.Label(
            header, text="규칙을 등록한 ChatGPT 채팅에 아래 짧은 질문을 붙여넣으세요",
            bg=COLORS["panel"], fg=COLORS["gold"], font=("Malgun Gothic", 13, "bold"),
        ).pack(side="left")
        actions = tk.Frame(header, bg=COLORS["panel"])
        actions.pack(side="right")
        self._button(actions, "짧은 질문 복사", self._copy_prompt, COLORS["purple"]).pack(side="left", padx=(0, 8))
        self._button(actions, "닫기", dialog.destroy, COLORS["muted"]).pack(side="left")
        text = tk.Text(
            dialog, bg="#09111f", fg=COLORS["text"], insertbackground=COLORS["text"],
            selectbackground="#29476f", relief="flat", bd=0, padx=14, pady=12,
            wrap="word", font=("Consolas", 9),
        )
        text.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        text.insert("1.0", prompt)
        text.configure(state="disabled")

    def _paste_clipboard_response(self) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("클립보드", "클립보드에 텍스트가 없습니다.", parent=self.root)
            return
        self.response_text.delete("1.0", "end")
        self.response_text.insert("1.0", text)
        self._apply_response()

    def _apply_response(self) -> None:
        text = self.response_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("답변 없음", "ChatGPT 답변을 붙여넣어 주세요.", parent=self.root)
            return
        self._apply_recommendation_text(text)

    def _apply_recommendation_text(
        self,
        text: str,
        *,
        show_dialog: bool = True,
        draft_context: DraftSnapshot | None = None,
        render_summary: bool = True,
    ) -> bool:
        parsed_draft = draft_context or self.draft
        repaired_unavailable = False
        try:
            recommendations = parse_response(text, parsed_draft, self.registry)
        except StaleResponseError as exc:
            self._recommendation_apply_error = str(exc)
            self.exchange_status.configure(text=str(exc), fg=COLORS["orange"])
            if show_dialog:
                messagebox.showwarning("오래된 추천", str(exc), parent=self.root)
            return False
        except UnavailableRecommendationError as exc:
            # A single banned/locked suggestion must not discard the other
            # valid Codex picks.  Keep those rows in their original order and
            # fill only the vacant slots from the already-cached local list.
            try:
                codex_recommendations = parse_response(
                    text, parsed_draft, self.registry, skip_unavailable=True,
                )
                blocked = set(parsed_draft.unavailable_champions())
                candidates = self._local_recommendation_candidates()
                candidate_ids = [item.champion_id for item in candidates]
                adc = allied_adc_member(parsed_draft)
                local_recommendations = local_recommendations_from_candidates(
                    candidates,
                    unavailable=blocked,
                    personal_stats=self._personal_stats_for(candidate_ids),
                    synergies={
                        champion_id: self._synergy_for(champion_id)
                        for champion_id in candidate_ids
                    },
                    enemy_name=(
                        self._champion_text(
                            parsed_draft.selected_enemy_support_id,
                            parsed_draft.selected_enemy_support_name_ko,
                        )
                        if parsed_draft.selected_enemy_support_id else ""
                    ),
                    ally_adc_name=(
                        self._champion_text(adc.champion_id, adc.champion_name_ko)
                        if adc else ""
                    ),
                    role_name=self._position_text(parsed_draft.my_role),
                    language=self.ui_language,
                )
                recommendations = merge_codex_with_local_recommendations(
                    codex_recommendations,
                    local_recommendations,
                    unavailable=blocked,
                )
            except ResponseError:
                recommendations = []
            if not recommendations:
                self._recommendation_apply_error = str(exc)
                self.exchange_status.configure(text=str(exc), fg=COLORS["red"])
                if show_dialog:
                    messagebox.showerror("추천 적용 실패", str(exc), parent=self.root)
                return False
            repaired_unavailable = True
        except ResponseError as exc:
            self._recommendation_apply_error = str(exc)
            self.exchange_status.configure(text=str(exc), fg=COLORS["red"])
            if show_dialog:
                messagebox.showerror("답변 형식 오류", str(exc), parent=self.root)
            return False
        self._recommendation_apply_error = ""
        self._recommendation_generation = (
            int(getattr(self, "_recommendation_generation", 0)) + 1
        )
        self.recommendations = recommendations
        self.recommendation_source = "CODEX"
        self.recommendation_snapshot_id = parsed_draft.snapshot_id
        self.recommendation_context_signature = (
            recommendation_draft_context_signature(parsed_draft)
        )
        self.recommendation_enemy_support_id = (
            parsed_draft.selected_enemy_support_id or ""
        )
        self.exchange_status.configure(
            text=(
                "사용 불가 후보 제외 · 유효한 Codex 추천 유지 · 빈자리 로컬 보충"
                if repaired_unavailable else self._text("recommendations.applied")
            ),
            fg=COLORS["orange"] if repaired_unavailable else COLORS["green"],
        )
        # Only two small panels depend on the response; rebuilding the whole
        # selection screen here caused a visible flash on every answer.
        self._render_recommendations()
        self._selection_panel_signatures.pop("prompt", None)
        if render_summary:
            self._render_prompt_summary()
        # Recommendations are advisory only. Never alter the League Client
        # merely because a Codex response arrived; the user must explicitly
        # press the card's "롤에 선택" button.
        return True

    def _refresh_opgg(self) -> None:
        if self._opgg_refreshing:
            return
        self._sync_ally_adc_synergy(force=True)
        enemy_support = self.draft.selected_enemy_support_id
        position = self.draft.my_role
        cached_meta = self.storage.load_opgg_snapshot(None, position)
        cached_matchup = (
            self.storage.load_opgg_snapshot(enemy_support, position)
            if enemy_support else cached_meta
        )
        refresh_meta = not (
            cached_meta and self._meta_snapshot_fresh(cached_meta)
        )
        refresh_matchup = bool(
            enemy_support and not (
                cached_matchup and self._matchup_snapshot_fresh(cached_matchup)
            )
        )
        if not refresh_meta and not refresh_matchup:
            meta_hours = self._data_preference("opgg_meta_cooldown_hours")
            matchup_hours = self._data_preference("opgg_matchup_cooldown_hours")
            self.exchange_status.configure(
                text=(
                    "메타·상성·조합 캐시 확인 완료 · "
                    f"메타 {meta_hours}시간 / 상성 {matchup_hours}시간 재사용"
                ),
                fg=COLORS["green"],
            )
            return
        self._opgg_refreshing = True
        self._render_header()

        def work() -> tuple[
            OpggSnapshot | None, OpggSnapshot | None, list[str], list[str]
        ]:
            meta_snapshot = cached_meta
            matchup_snapshot = cached_matchup
            errors: list[str] = []
            refreshed: list[str] = []
            if refresh_meta:
                try:
                    meta_snapshot = self.opgg_client.refresh_overall(position)
                    refreshed.append("포지션 메타")
                except Exception as exc:
                    errors.append(f"포지션 순위: {exc}")
            if refresh_matchup and enemy_support:
                try:
                    matchup_snapshot = self.opgg_client.refresh_matchup(
                        enemy_support, position
                    )
                    refreshed.append("상대 상성")
                except Exception as exc:
                    errors.append(f"상대 상성: {exc}")
            elif not enemy_support:
                matchup_snapshot = meta_snapshot
            if not meta_snapshot and not matchup_snapshot:
                raise OpggError(" / ".join(errors) or "OP.GG 데이터를 읽지 못했습니다.")
            return meta_snapshot, matchup_snapshot, errors, refreshed

        def success(
            result: tuple[
                OpggSnapshot | None, OpggSnapshot | None, list[str], list[str]
            ]
        ) -> None:
            meta_snapshot, matchup_snapshot, errors, refreshed = result
            self._opgg_refreshing = False
            if meta_snapshot and refresh_meta:
                self.storage.save_opgg_snapshot(meta_snapshot)
            if meta_snapshot:
                self.opgg_meta_snapshot = meta_snapshot
            if matchup_snapshot and refresh_matchup:
                self.storage.save_opgg_snapshot(matchup_snapshot)
            if matchup_snapshot:
                self.opgg_snapshot = matchup_snapshot
            self.exchange_status.configure(
                text=(
                    "OP.GG 일부 갱신 · " + " / ".join(errors)
                    if errors else
                    f"{', '.join(refreshed) or '로컬 캐시'} 확인 완료 · 설정 주기 재사용"
                ),
                fg=COLORS["orange"] if errors else COLORS["blue"],
            )
            self._render_selection()

        def error(exc: Exception) -> None:
            self._opgg_refreshing = False
            self.exchange_status.configure(text=str(exc), fg=COLORS["red"])
            self._render_header()
            messagebox.showerror("OP.GG 갱신 실패", str(exc), parent=self.root)

        self._background(work, success, error)

    def _sync_hover_matchup(self) -> None:
        """Refresh the current local selection while keeping cached data visible."""
        hover = local_draft_selection(self.draft)
        enemy_champion_id = self.draft.selected_enemy_support_id
        position = self.draft.my_role
        self._load_hover_personal_stat()
        if not hover or not hover.champion_id:
            self._hover_personal_context = None
            self._hover_personal_stat = None
            self._schedule_hover_matchup_render()
            return
        if self.demo or not enemy_champion_id:
            self._schedule_hover_matchup_render()
            return
        cached = self.storage.load_opgg_snapshot(enemy_champion_id, position)
        if cached:
            self.opgg_snapshot = cached
        self._schedule_hover_matchup_render()
        if not cached or not self._matchup_snapshot_fresh(cached):
            self._sync_selected_matchup()

    def _load_hover_personal_stat(self) -> None:
        hover = local_draft_selection(self.draft)
        puuid = self.storage.get_setting("riot_puuid")
        if not hover or not hover.champion_id or not puuid:
            self._hover_personal_context = None
            self._hover_personal_stat = None
            return
        adc = allied_adc_member(self.draft) if self.draft.my_role == "SUPPORT" else None
        champion_id = hover.champion_id
        enemy_champion_id = self.draft.selected_enemy_support_id
        position = self.draft.my_role
        ally_adc_id = adc.champion_id if adc else None
        context = (
            self.storage.match_revision(), puuid, position,
            champion_id, enemy_champion_id, ally_adc_id,
        )
        if context == self._hover_personal_context:
            return
        self._hover_personal_context = context
        self._hover_personal_stat = None
        self._hover_personal_loading.add(context)
        self._schedule_hover_matchup_render()

        def success(stats: dict[str, PersonalStat]) -> None:
            self._hover_personal_loading.discard(context)
            if context == self._hover_personal_context:
                self._hover_personal_stat = stats.get(champion_id, PersonalStat())
                self._schedule_hover_matchup_render()

        def error(_exc: Exception) -> None:
            self._hover_personal_loading.discard(context)
            if context == self._hover_personal_context:
                self._hover_personal_stat = PersonalStat()
                self._schedule_hover_matchup_render()

        self._background(
            lambda: self.storage.personal_stats(
                puuid, [champion_id], enemy_champion_id,
                ally_adc_id, limit=1000, position=position,
            ),
            success, error,
        )

    def _sync_selected_matchup(self) -> None:
        """Show cached matchup data first and honor its configured refresh TTL."""
        if self.demo:
            return
        enemy_champion_id = self.draft.selected_enemy_support_id
        position = self.draft.my_role
        if not enemy_champion_id:
            self._schedule_hover_matchup_render()
            return
        cached = self.storage.load_opgg_snapshot(enemy_champion_id, position)
        if cached:
            self.opgg_snapshot = cached
            self._schedule_selection_render()
        if cached and self._matchup_snapshot_fresh(cached):
            self._schedule_hover_matchup_render()
            return
        cache_key = f"{position}:{enemy_champion_id}".upper()
        if cache_key in self._selection_matchup_refreshing:
            self._schedule_hover_matchup_render()
            return
        self._selection_matchup_refreshing.add(cache_key)
        self._hover_matchup_errors.pop(cache_key, None)
        self._schedule_hover_matchup_render()

        def work() -> OpggSnapshot:
            return self.opgg_client.refresh_matchup(enemy_champion_id, position)

        def success(snapshot: OpggSnapshot) -> None:
            self._selection_matchup_refreshing.discard(cache_key)
            self._hover_matchup_errors.pop(cache_key, None)
            self.storage.save_opgg_snapshot(snapshot)
            if (
                self.draft.my_role == position
                and self.draft.selected_enemy_support_id == enemy_champion_id
            ):
                self.opgg_snapshot = snapshot
                self.exchange_status.configure(
                    text=(
                        f"{self.registry.ko_name(enemy_champion_id)} 상대 상성 갱신 · "
                        f"다음 자동 요청은 {self._data_preference('opgg_matchup_cooldown_hours')}시간 뒤"
                    ),
                    fg=COLORS["green"],
                )
                self._schedule_selection_render()
            self._schedule_hover_matchup_render()

        def error(exc: Exception) -> None:
            self._selection_matchup_refreshing.discard(cache_key)
            self._hover_matchup_errors[cache_key] = str(exc)
            if (
                not cached
                and self.draft.my_role == position
                and self.draft.selected_enemy_support_id == enemy_champion_id
            ):
                self.exchange_status.configure(
                    text=f"상성 갱신 실패 · 저장된 값 없음 · {exc}",
                    fg=COLORS["orange"],
                )
            self._schedule_hover_matchup_render()

        self._background(work, success, error)

    def _sync_position_meta(self) -> None:
        """Use position meta from disk first and honor its configured refresh TTL."""
        if self.demo:
            return
        position = self.draft.my_role
        cached = self.storage.load_opgg_snapshot(None, position)
        if cached:
            self.opgg_meta_snapshot = cached
            if not self.draft.selected_enemy_support_id:
                self.opgg_snapshot = cached
            self._schedule_selection_render()
        if cached and self._meta_snapshot_fresh(cached):
            return
        cache_key = f"META:{position}".upper()
        if cache_key in self._selection_matchup_refreshing:
            return
        self._selection_matchup_refreshing.add(cache_key)

        def work() -> OpggSnapshot:
            return self.opgg_client.refresh_overall(position)

        def success(snapshot: OpggSnapshot) -> None:
            self._selection_matchup_refreshing.discard(cache_key)
            self.storage.save_opgg_snapshot(snapshot)
            if self.draft.my_role == position:
                self.opgg_meta_snapshot = snapshot
                if not self.draft.selected_enemy_support_id:
                    self.opgg_snapshot = snapshot
                self._schedule_selection_render()

        def error(_exc: Exception) -> None:
            self._selection_matchup_refreshing.discard(cache_key)

        self._background(work, success, error)

    def _sync_ally_adc_synergy(self, force: bool = False) -> None:
        if self.demo:
            return
        adc = allied_adc_member(self.draft)
        if self.draft.my_role != "SUPPORT" or not adc:
            self.opgg_synergy_snapshot = None
            self._synergy_checked_adc = ""
            self._schedule_selection_render()
            return
        adc_id = adc.champion_id
        fresh = self.storage.load_opgg_synergy_snapshot(
            adc_id,
            max_age=self._request_max_age("opgg_synergy_cooldown_hours"),
        )
        cached = fresh or self.storage.load_opgg_synergy_snapshot(adc_id)
        if cached:
            self.opgg_synergy_snapshot = cached
            self._schedule_selection_render()
        elif not self.opgg_synergy_snapshot or (
            self.opgg_synergy_snapshot.ally_champion_id != adc_id
        ):
            self.opgg_synergy_snapshot = None
        if fresh:
            self._synergy_checked_adc = adc_id
            return
        if self._synergy_refreshing:
            return
        if self._synergy_checked_adc == adc_id:
            return
        self._synergy_refreshing = True
        self._synergy_checked_adc = adc_id
        self._schedule_selection_render()

        def work() -> OpggSynergySnapshot:
            return OpggMcpClient(timeout=18.0).champion_synergies(
                adc_id, my_position="adc", synergy_position="support",
                lang="ko_KR", key_resolver=self.registry.from_key,
            )

        def success(snapshot: OpggSynergySnapshot) -> None:
            self._synergy_refreshing = False
            self.storage.save_opgg_synergy_snapshot(snapshot)
            current_adc = allied_adc_member(self.draft)
            if current_adc and current_adc.champion_id == snapshot.ally_champion_id:
                self.opgg_synergy_snapshot = snapshot
                self._prompt_copied_snapshot_id = ""
                self.exchange_status.configure(
                    text=(
                        f"{current_adc.champion_name_ko} 원딜 조합 통계 "
                        f"{len(snapshot.synergies)}개 갱신 완료"
                    ),
                    fg=COLORS["green"],
                )
            self._schedule_selection_render()

        def error(exc: Exception) -> None:
            self._synergy_refreshing = False
            if not cached:
                self.exchange_status.configure(
                    text=f"원딜 조합 통계 갱신 실패 · {exc}", fg=COLORS["orange"],
                )
            self._schedule_selection_render()

        self._background(work, success, error)

    def _ensure_codex_cli_client(self) -> CodexCliClient:
        if self.codex_cli is None:
            self.codex_cli = CodexCliClient(
                self.storage.db_path.parent / "codex_dialog"
            )
            self._codex_cli_error = ""
        return self.codex_cli

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(self._tr("Riot API · Codex CLI 설정"))
        dialog.configure(bg=COLORS["panel"])
        dialog.geometry("760x780")
        dialog.minsize(680, 620)
        dialog.transient(self.root)
        dialog.grab_set()
        shell = tk.Frame(dialog, bg=COLORS["panel"])
        shell.pack(fill="both", expand=True)
        settings_canvas = tk.Canvas(
            shell, bg=COLORS["panel"], highlightthickness=0,
        )
        settings_scrollbar = ttk.Scrollbar(
            shell, orient="vertical", command=settings_canvas.yview,
        )
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_scrollbar.pack(side="right", fill="y")
        settings_canvas.pack(side="left", fill="both", expand=True)
        settings_body = tk.Frame(settings_canvas, bg=COLORS["panel"])
        settings_window = settings_canvas.create_window(
            (0, 0), window=settings_body, anchor="nw",
        )
        settings_body.bind(
            "<Configure>",
            lambda _event: settings_canvas.configure(
                scrollregion=settings_canvas.bbox("all")
            ),
        )
        settings_canvas.bind(
            "<Configure>",
            lambda event: settings_canvas.itemconfigure(
                settings_window, width=event.width
            ),
        )
        dialog._advisor_scroll_canvas = settings_canvas
        fields = [
            ("Riot 게임 이름", "riot_game_name", False),
            ("태그", "riot_tag_line", False),
            ("Riot API 키", "riot_api_key", True),
            ("Codex thread_id", "codex_thread_id", False),
        ]
        entries: dict[str, tk.Entry] = {}
        tk.Label(settings_body, text=self._tr("Riot 전적 · Codex 추천 설정"), bg=COLORS["panel"], fg=COLORS["gold"],
                 font=("Malgun Gothic", 14, "bold")).pack(anchor="w", padx=20, pady=(18, 12))
        form = tk.Frame(settings_body, bg=COLORS["panel"])
        form.pack(fill="x", padx=20)
        for row, (label, key, secret) in enumerate(fields):
            tk.Label(form, text=self._tr(label), bg=COLORS["panel"], fg=COLORS["muted"], width=15,
                     anchor="w", font=("Malgun Gothic", 9)).grid(row=row, column=0, sticky="w", pady=6)
            entry = tk.Entry(form, bg="#0b1220", fg=COLORS["text"], insertbackground=COLORS["text"],
                             relief="flat", show="*" if secret else "", font=("Malgun Gothic", 10))
            if not secret:
                entry.insert(0, self.storage.get_setting(key))
            entry.grid(row=row, column=1, sticky="ew", pady=6, ipady=7)
            entries[key] = entry
        form.grid_columnconfigure(1, weight=1)
        tk.Label(
            settings_body,
            text=self._tr(
                "게임 이름과 태그는 롤 클라이언트에서 자동 감지됩니다.\n"
                "API 키가 이미 저장되어 있으면 입력란은 비워 두세요. 새 키만 입력하면 교체됩니다.\n"
                "Codex thread_id와 개발 키는 이 PC의 data/advisor.db에만 저장됩니다."
            ),
            bg=COLORS["panel"], fg=COLORS["orange"], font=("Malgun Gothic", 8),
        ).pack(anchor="w", padx=20, pady=(8, 12))

        language_outer = tk.Frame(
            settings_body, bg=COLORS["border"], padx=1, pady=1,
        )
        language_outer.pack(fill="x", padx=20, pady=(0, 12))
        language_panel = tk.Frame(
            language_outer, bg=COLORS["panel_2"], padx=14, pady=11,
        )
        language_panel.pack(fill="x")
        tk.Label(
            language_panel, text=self._tr("화면 언어"),
            bg=COLORS["panel_2"], fg=COLORS["blue"],
            font=("Malgun Gothic", 9, "bold"), width=15, anchor="w",
        ).pack(side="left")
        language_var = tk.StringVar(
            value=LANGUAGE_LABELS.get(self.ui_language, "한국어")
        )
        language_combo = ttk.Combobox(
            language_panel, textvariable=language_var,
            values=[LANGUAGE_LABELS["ko"], LANGUAGE_LABELS["en"]],
            state="readonly", width=16, font=("Malgun Gothic", 9),
        )
        language_combo.pack(side="left", ipady=4)
        tk.Label(
            language_panel,
            text=self._tr("언어 변경은 저장 즉시 적용되고 다음 실행에도 유지됩니다."),
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        ).pack(side="left", padx=(12, 0))

        feature_outer = tk.Frame(
            settings_body, bg=COLORS["border"], padx=1, pady=1,
        )
        feature_outer.pack(fill="x", padx=20, pady=(0, 12))
        feature_panel = tk.Frame(
            feature_outer, bg=COLORS["panel_2"], padx=14, pady=12,
        )
        feature_panel.pack(fill="x")
        tk.Label(
            feature_panel, text=self._tr("자동화 · AI 기능 허용"),
            bg=COLORS["panel_2"], fg=COLORS["purple"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            feature_panel,
            text=self._tr(
                "픽 추천은 명시적으로 허용한 경우에만 선택창에 표시되고 Codex CLI로 전송됩니다. "
                "자동 밴은 아래에서 선택한 챔피언을 사용합니다."
            ),
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8), justify="left", wraplength=680,
        ).pack(anchor="w", pady=(4, 9))
        feature_grid = tk.Frame(feature_panel, bg=COLORS["panel_2"])
        feature_grid.pack(fill="x")
        codex_enabled_var = tk.BooleanVar(
            value=bool(self.codex_recommendations_enabled)
        )
        codex_check = tk.Checkbutton(
            feature_grid, text=self._tr("픽 추천 사용 (Codex CLI)"),
            variable=codex_enabled_var,
            bg=COLORS["panel_2"], fg=COLORS["text"],
            activebackground=COLORS["panel_2"], activeforeground=COLORS["text"],
            selectcolor=COLORS["surface_selected"],
            font=("Malgun Gothic", 9, "bold"), cursor="hand2",
        )
        codex_check.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 9))

        stop_queue_after_dodge_var = tk.BooleanVar(
            value=bool(self.stop_queue_after_dodge_enabled)
        )
        tk.Checkbutton(
            feature_grid, text=self._tr("닷지 후 게임찾기 자동 중단"),
            variable=stop_queue_after_dodge_var,
            bg=COLORS["panel_2"], fg=COLORS["text"],
            activebackground=COLORS["panel_2"], activeforeground=COLORS["text"],
            selectcolor=COLORS["surface_selected"],
            font=("Malgun Gothic", 9, "bold"), cursor="hand2",
        ).grid(row=1, column=0, columnspan=2, sticky="w")
        tk.Label(
            feature_grid,
            text=self._tr("기본 OFF · 닷지 후 자동으로 다시 큐를 잡지 않습니다."),
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8),
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 9))

        tk.Label(
            feature_grid, text=self._tr("자동 밴 챔피언"),
            bg=COLORS["panel_2"], fg=COLORS["text"],
            font=("Malgun Gothic", 9, "bold"), width=15, anchor="w",
        ).grid(row=3, column=0, sticky="w")
        auto_ban_rows = sorted(
            self.registry.by_id.items(),
            key=(
                (lambda item: item[0].casefold())
                if self.ui_language == "en"
                else (lambda item: item[1][1])
            ),
        )
        auto_ban_choices = [
            (
                champion_id if self.ui_language == "en"
                else name_ko,
                int(champion_key),
            )
            for champion_id, (champion_key, name_ko) in auto_ban_rows
        ]
        auto_ban_key_by_label = dict(auto_ban_choices)
        selected_auto_ban_label = next(
            (
                label for label, key in auto_ban_choices
                if key == self._auto_ban_champion()[0]
            ),
            "Lux" if self.ui_language == "en" else "럭스",
        )
        auto_ban_choice_var = tk.StringVar(value=selected_auto_ban_label)
        auto_ban_combo = ttk.Combobox(
            feature_grid, textvariable=auto_ban_choice_var,
            values=[label for label, _key in auto_ban_choices],
            state="readonly", width=34, font=("Malgun Gothic", 9),
        )
        auto_ban_combo.grid(row=3, column=1, sticky="ew", padx=(8, 0), ipady=4)
        feature_grid.columnconfigure(1, weight=1)

        data_outer = tk.Frame(
            settings_body, bg=COLORS["border"], padx=1, pady=1,
        )
        data_outer.pack(fill="x", padx=20, pady=(0, 12))
        data_panel = tk.Frame(
            data_outer, bg=COLORS["panel_2"], padx=14, pady=12,
        )
        data_panel.pack(fill="x")
        data_heading = tk.Frame(data_panel, bg=COLORS["panel_2"])
        data_heading.pack(fill="x")
        tk.Label(
            data_heading, text=self._tr("데이터 재사용 · 표시 설정"),
            bg=COLORS["panel_2"], fg=COLORS["green"],
            font=("Malgun Gothic", 10, "bold"),
        ).pack(side="left")
        tk.Label(
            data_panel,
            text=self._tr(
                "오래된 로컬 데이터는 즉시 보여 주고, 아래 시간이 지난 뒤에만 "
                "같은 외부 요청을 다시 보냅니다. (24시간=1일, 재요청 1~720시간 · 메타 1~20개)"
            ),
            bg=COLORS["panel_2"], fg=COLORS["muted"],
            font=("Malgun Gothic", 8), justify="left",
        ).pack(anchor="w", pady=(4, 9))
        data_grid = tk.Frame(data_panel, bg=COLORS["panel_2"])
        data_grid.pack(fill="x")
        data_grid.columnconfigure(0, weight=1, uniform="data_setting")
        data_grid.columnconfigure(1, weight=1, uniform="data_setting")
        data_entries: dict[str, tk.Entry] = {}
        for index, (
            key, label, unit, _default, _minimum, _maximum,
        ) in enumerate(DATA_PREFERENCE_SPECS):
            card = tk.Frame(data_grid, bg=COLORS["panel_2"])
            card.grid(
                row=index // 2, column=index % 2, sticky="ew",
                padx=(0, 12) if index % 2 == 0 else (12, 0), pady=4,
            )
            tk.Label(
                card, text=self._tr(label), bg=COLORS["panel_2"], fg=COLORS["text"],
                font=("Malgun Gothic", 8, "bold"), width=21, anchor="w",
            ).pack(side="left")
            entry = tk.Entry(
                card, bg=COLORS["surface"], fg=COLORS["text"],
                insertbackground=COLORS["text"], relief="flat",
                justify="right", width=5, font=("Malgun Gothic", 9, "bold"),
            )
            entry.insert(0, str(self._data_preference(key)))
            entry.pack(side="left", ipady=4)
            tk.Label(
                card, text=self._tr(unit), bg=COLORS["panel_2"], fg=COLORS["muted"],
                font=("Malgun Gothic", 8),
            ).pack(side="left", padx=(5, 0))
            data_entries[key] = entry

        def reset_data_preferences() -> None:
            for key, (_default, _minimum, _maximum) in DATA_PREFERENCE_LIMITS.items():
                entry = data_entries[key]
                entry.delete(0, "end")
                entry.insert(0, str(DATA_PREFERENCE_LIMITS[key][0]))

        self._button(
            data_heading, "기본값 복원", reset_data_preferences,
            COLORS["muted"], width=11,
        ).pack(side="right")

        self._button(
            settings_body, "Riot Developer Portal 열기", self._open_developer_portal, COLORS["orange"]
        ).pack(anchor="w", padx=20, pady=(0, 10))

        validation_status = tk.Label(
            settings_body, text="", bg=COLORS["panel"], fg=COLORS["muted"],
            font=("Malgun Gothic", 9, "bold"),
        )
        validation_status.pack(anchor="w", padx=20, pady=(0, 8))

        def set_codex_controls_enabled(enabled: bool) -> None:
            state = "normal" if enabled else "disabled"
            codex_status_button.configure(state=state)
            memory_send_button.configure(
                state=(
                    "normal"
                    if enabled and bool(codex_enabled_var.get()) and not self.demo
                    else "disabled"
                )
            )

        def check_codex_status() -> None:
            try:
                client = self._ensure_codex_cli_client()
            except CodexCliError as exc:
                validation_status.configure(text=str(exc), fg=COLORS["red"])
                return
            set_codex_controls_enabled(False)
            validation_status.configure(
                text="Codex CLI 설치·로그인 상태 확인 중…", fg=COLORS["blue"]
            )

            def success(result: tuple[str, str]) -> None:
                if not dialog.winfo_exists():
                    return
                version, login = result
                set_codex_controls_enabled(True)
                color = COLORS["green"] if "ChatGPT" in login else COLORS["orange"]
                validation_status.configure(
                    text=f"{version} · {login}", fg=color,
                )

            def error(exc: Exception) -> None:
                if not dialog.winfo_exists():
                    return
                set_codex_controls_enabled(True)
                validation_status.configure(text=str(exc), fg=COLORS["red"])

            self._background(
                lambda: (client.version(), client.login_status()), success, error,
            )

        def register_codex_memory() -> None:
            if self.demo:
                validation_status.configure(
                    text="데모에서는 Codex CLI로 내용을 전송하지 않습니다.",
                    fg=COLORS["orange"],
                )
                return
            if not codex_enabled_var.get():
                validation_status.configure(
                    text="먼저 ‘픽 추천 사용 (Codex CLI)’을 허용하세요.",
                    fg=COLORS["orange"],
                )
                return
            if self._codex_cli_running:
                validation_status.configure(
                    text="Codex CLI 요청이 이미 진행 중입니다.", fg=COLORS["orange"]
                )
                return
            try:
                client = self._ensure_codex_cli_client()
            except CodexCliError as exc:
                validation_status.configure(text=str(exc), fg=COLORS["red"])
                return
            requested_thread_id = entries["codex_thread_id"].get().strip()
            self._codex_cli_running = True
            set_codex_controls_enabled(False)
            validation_status.configure(
                text=(
                    "기존 대화에 1회 규칙을 보내는 중…"
                    if requested_thread_id else
                    "새 Codex 대화를 만들고 1회 규칙을 보내는 중…"
                ),
                fg=COLORS["blue"],
            )
            self._selection_panel_signatures.pop("prompt", None)
            self._render_prompt_summary()

            def success(turn: CodexTurn) -> None:
                self._codex_cli_running = False
                self.storage.set_setting("codex_thread_id", turn.thread_id)
                self.storage.set_setting("codex_memory_thread_id", turn.thread_id)
                self.storage.set_setting("codex_memory_version", MEMORY_PROMPT_VERSION)
                if dialog.winfo_exists():
                    entries["codex_thread_id"].delete(0, "end")
                    entries["codex_thread_id"].insert(0, turn.thread_id)
                    set_codex_controls_enabled(True)
                    validation_status.configure(
                        text=(
                            f"1회 규칙 등록 완료 · thread_id 자동 저장 · "
                            f"{turn.thread_id} · {turn.model}"
                        ),
                        fg=COLORS["green"],
                    )
                self._selection_panel_signatures.pop("prompt", None)
                self._render_prompt_summary()

            def error(exc: Exception) -> None:
                self._codex_cli_running = False
                self._codex_cli_error = str(exc)
                if dialog.winfo_exists():
                    set_codex_controls_enabled(True)
                    validation_status.configure(text=str(exc), fg=COLORS["red"])
                self._selection_panel_signatures.pop("prompt", None)
                self._render_prompt_summary()

            self._background(
                lambda: client.register_memory(
                    build_memory_prompt(), requested_thread_id,
                ),
                success,
                error,
            )

        codex_controls = tk.Frame(settings_body, bg=COLORS["panel"])
        codex_controls.pack(fill="x", padx=20, pady=(0, 12))
        memory_send_button = self._button(
            codex_controls, "1회 규칙 보내기", register_codex_memory,
            COLORS["purple"], width=18, filled=True,
        )
        memory_send_button.pack(side="left", padx=(0, 8))
        codex_status_button = self._button(
            codex_controls, "CLI 로그인 확인", check_codex_status,
            COLORS["blue"], width=17,
        )
        codex_status_button.pack(side="left")
        codex_check.configure(
            command=lambda: set_codex_controls_enabled(True)
        )
        set_codex_controls_enabled(True)
        tk.Label(
            codex_controls,
            text="빈 thread_id면 새 대화를 만들고 자동 입력합니다.",
            bg=COLORS["panel"], fg=COLORS["muted"], font=("Malgun Gothic", 8),
        ).pack(side="left", padx=(12, 0))
        dialog.after(120, check_codex_status)

        def read_data_preferences() -> dict[str, int] | None:
            values: dict[str, int] = {}
            labels = {
                key: label for key, label, _unit, _default, _minimum, _maximum
                in DATA_PREFERENCE_SPECS
            }
            for key, (default, minimum, maximum) in DATA_PREFERENCE_LIMITS.items():
                raw = data_entries[key].get().strip()
                try:
                    value = int(raw)
                except ValueError:
                    value = default
                    valid = False
                else:
                    valid = minimum <= value <= maximum
                if not valid:
                    label = labels[key]
                    message = f"{label}: {minimum}~{maximum} 사이의 정수를 입력하세요."
                    validation_status.configure(text=message, fg=COLORS["red"])
                    messagebox.showwarning("설정값 확인", message, parent=dialog)
                    data_entries[key].focus_set()
                    return None
                values[key] = value
            return values

        def finish_save(
            game_name: str, tag_line: str, new_api_key: str,
            puuid: str = "", codex_thread_id: str = "",
            data_preferences: dict[str, int] | None = None,
            codex_recommendations_enabled: bool = False,
            stop_queue_after_dodge_enabled: bool = False,
            auto_ban_champion_key: int = 99,
            ui_language: str = "ko",
        ) -> None:
            self.storage.set_setting("riot_game_name", game_name)
            self.storage.set_setting("riot_tag_line", tag_line)
            if new_api_key:
                self.storage.set_riot_api_key(new_api_key)
                with self._live_identity_lock:
                    self._live_identity_resolution_auth_failed = False
            if puuid:
                self.storage.set_setting("riot_puuid", puuid)
            previous_thread_id = self.storage.get_setting("codex_thread_id")
            self.storage.set_setting("codex_thread_id", codex_thread_id)
            if (
                previous_thread_id != codex_thread_id
                and self.storage.get_setting("codex_memory_thread_id") != codex_thread_id
            ):
                self.storage.set_setting("codex_memory_thread_id", "")
                self.storage.set_setting("codex_memory_version", "")
            for key, value in (data_preferences or {}).items():
                self.storage.set_setting(key, str(value))
            self.codex_recommendations_enabled = bool(
                codex_recommendations_enabled
            )
            self.storage.set_setting(
                "codex_recommendations_enabled",
                "1" if self.codex_recommendations_enabled else "0",
            )
            self.stop_queue_after_dodge_enabled = bool(
                stop_queue_after_dodge_enabled
            )
            self.storage.set_setting(
                "stop_queue_after_dodge_enabled",
                "1" if self.stop_queue_after_dodge_enabled else "0",
            )
            self.ui_language = normalize_language(ui_language)
            self.storage.set_setting("ui_language", self.ui_language)
            self._set_auto_ban_champion(auto_ban_champion_key)
            self._reload_data_preferences()
            self._tab_build_refresh_attempted.clear()
            self._synergy_checked_adc = ""
            self._opgg_profiles_checked_signature = ""
            self._selection_panel_signatures.clear()
            self._hover_matchup_signature = ""
            self._lux_auto_ban_display_signature = None
            self._apply_codex_recommendation_visibility()
            dialog.destroy()
            status_text = (
                "Riot API 키 검증 및 저장 완료" if new_api_key else "Riot 설정 저장 완료"
            )
            self.exchange_status.configure(text=status_text, fg=COLORS["green"])
            self._render_header()
            if self._current_main_tab_index() == 0:
                self._render_selection()
            else:
                self._render_opgg_meta()
            self._apply_language(full=True)
            if (
                self._cache_manager_window
                and self._cache_manager_window.winfo_exists()
            ):
                self._refresh_cache_manager_rows()
                self._refresh_cache_manager_champion_cards()
            if self.live_game.players and not self._profiles_loading:
                self.player_profiles = {
                    player.riot_id: PlayerProfileStat(status="LOADING")
                    for player in self.live_game.players
                }
                self._load_live_profiles()
                self._duo_checked_signature = ""
                self.root.after(250, self._check_live_duos)
                self.root.after(350, self._load_opgg_live_profiles)
                self._start_live_identity_capture()
            self.root.after(150, lambda: self._sync_riot(automatic=True))

        def save() -> None:
            game_name = entries["riot_game_name"].get().strip()
            tag_line = entries["riot_tag_line"].get().strip()
            new_api_key = entries["riot_api_key"].get().strip()
            codex_thread_id = entries["codex_thread_id"].get().strip()
            data_preferences = read_data_preferences()
            if data_preferences is None:
                return
            selected_auto_ban_key = auto_ban_key_by_label.get(
                auto_ban_choice_var.get(), 99,
            )
            codex_allowed = bool(codex_enabled_var.get())
            stop_after_dodge = bool(stop_queue_after_dodge_var.get())
            selected_language = next(
                (
                    code for code, label in LANGUAGE_LABELS.items()
                    if label == language_var.get()
                ),
                "ko",
            )
            if not new_api_key:
                finish_save(
                    game_name, tag_line, "",
                    codex_thread_id=codex_thread_id,
                    data_preferences=data_preferences,
                    codex_recommendations_enabled=codex_allowed,
                    stop_queue_after_dodge_enabled=stop_after_dodge,
                    auto_ban_champion_key=selected_auto_ban_key,
                    ui_language=selected_language,
                )
                return
            if not game_name or not tag_line:
                validation_status.configure(
                    text="검증 불가 · 롤 클라이언트 연결 후 Riot ID와 태그를 확인하세요.",
                    fg=COLORS["red"],
                )
                messagebox.showwarning(
                    "API 키 검증 불가",
                    "API 키를 확인하려면 Riot 게임 이름과 태그가 필요합니다.",
                    parent=dialog,
                )
                return
            save_button.configure(state="disabled", text="API 키 확인 중...")
            validation_status.configure(
                text="Riot 서버에서 API 키를 확인하는 중입니다...", fg=COLORS["blue"]
            )

            def success(puuid: str) -> None:
                if not dialog.winfo_exists():
                    return
                validation_status.configure(
                    text="API 키 확인 성공 · 저장합니다.", fg=COLORS["green"]
                )
                finish_save(
                    game_name, tag_line, new_api_key,
                    puuid=puuid,
                    codex_thread_id=codex_thread_id,
                    data_preferences=data_preferences,
                    codex_recommendations_enabled=codex_allowed,
                    stop_queue_after_dodge_enabled=stop_after_dodge,
                    auto_ban_champion_key=selected_auto_ban_key,
                    ui_language=selected_language,
                )

            def error(exc: Exception) -> None:
                if not dialog.winfo_exists():
                    return
                save_button.configure(state="normal", text="검증 후 저장")
                validation_status.configure(
                    text=f"API 키 검증 실패 · {exc}", fg=COLORS["red"]
                )
                messagebox.showerror(
                    "API 키 검증 실패",
                    f"새 API 키를 저장하지 않았습니다.\n\n{exc}",
                    parent=dialog,
                )

            self._background(
                lambda: RiotApiClient(new_api_key).validate_key_for_account(
                    game_name, tag_line
                ),
                success,
                error,
            )

        save_button = self._button(
            settings_body, "검증 후 저장", save, COLORS["green"], width=14
        )
        save_button.pack(anchor="e", padx=20, pady=(0, 22))

    @staticmethod
    def _open_developer_portal() -> None:
        webbrowser.open("https://developer.riotgames.com/")

    def _cached_owner_solo_entry(
        self, puuid_hint: str = "",
    ) -> tuple[str, dict[str, object]]:
        """Read the owner's last Riot solo entry without making a request."""
        game_name = self.storage.get_setting("riot_game_name")
        tag_line = self.storage.get_setting("riot_tag_line")
        riot_id = f"{game_name}#{tag_line}" if game_name and tag_line else ""
        cached = (
            self.storage.load_live_profile_any_age(riot_id) if riot_id else None
        )
        if not cached:
            return "", {}
        cached_puuid, payload, _updated_at = cached
        if puuid_hint and cached_puuid and cached_puuid != puuid_hint:
            return "", {}
        puuid = str(
            puuid_hint
            or cached_puuid
            or self.storage.get_setting("riot_puuid")
        ).strip()
        entry = payload.get("solo_entry") or {}
        return puuid, dict(entry) if isinstance(entry, dict) else {}

    def _schedule_non_owner_prune(self, delay_ms: int = 3_600_000) -> None:
        if self.demo:
            return
        if self._non_owner_prune_after_id:
            try:
                self.root.after_cancel(self._non_owner_prune_after_id)
            except tk.TclError:
                pass
        self._non_owner_prune_after_id = self.root.after(
            max(int(delay_ms), 100), self._prune_non_owner_data_background,
        )

    def _prune_non_owner_data_background(self) -> None:
        """Expire only other-player caches; the owner's history is permanent."""
        self._non_owner_prune_after_id = None
        if self.demo or self._non_owner_prune_running:
            return
        owner_puuid = self.storage.get_setting("riot_puuid").strip()
        game_name = self.storage.get_setting("riot_game_name").strip()
        tag_line = self.storage.get_setting("riot_tag_line").strip()
        owner_riot_id = f"{game_name}#{tag_line}" if game_name and tag_line else ""
        if not owner_puuid or not owner_riot_id:
            self._schedule_non_owner_prune(600_000)
            return
        last_text = self.storage.get_setting("non_owner_pruned_at_1d").strip()
        if last_text:
            try:
                elapsed = datetime.now() - datetime.fromisoformat(last_text)
                if elapsed < timedelta(hours=1):
                    remaining_ms = int(
                        (timedelta(hours=1) - elapsed).total_seconds() * 1000
                    )
                    self._schedule_non_owner_prune(remaining_ms)
                    return
            except ValueError:
                pass
        self._non_owner_prune_running = True

        def work() -> dict[str, int]:
            return self.storage.prune_non_owner_data(
                owner_puuid, owner_riot_id, days=1,
            )

        def success(_deleted: dict[str, int]) -> None:
            self._non_owner_prune_running = False
            self.storage.set_setting(
                "non_owner_pruned_at_1d",
                datetime.now().isoformat(timespec="seconds"),
            )
            self._schedule_non_owner_prune()

        def error(_exc: Exception) -> None:
            self._non_owner_prune_running = False
            self._schedule_non_owner_prune(600_000)

        self._background(work, success, error)

    def _capture_pre_game_rank_snapshot(self, force_new: bool = False) -> bool:
        """Capture the cached pre-game rank before the live phase begins."""
        if self.demo:
            return False
        puuid, solo_entry = self._cached_owner_solo_entry()
        if not puuid or not solo_entry:
            return False
        session_key = self.storage.get_setting("rank_snapshot_active_session")
        session_puuid = self.storage.get_setting("rank_snapshot_active_puuid")
        if force_new or not session_key or session_puuid != puuid:
            draft_token = str(getattr(self.draft, "snapshot_id", "") or "")[:12]
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
            session_key = f"solo-{stamp}-{draft_token or 'live'}"
        snapshot = self.storage.save_rank_snapshot(
            puuid, solo_entry, stage="PRE", session_key=session_key,
        )
        if snapshot is None:
            return False
        self.storage.set_setting("rank_snapshot_active_session", session_key)
        self.storage.set_setting("rank_snapshot_active_puuid", puuid)
        return True

    def _finalize_pending_rank_snapshot(self, puuid: str) -> int:
        """Save the synchronized post state and resolve one unambiguous match."""
        session_key = self.storage.get_setting("rank_snapshot_active_session")
        session_puuid = self.storage.get_setting("rank_snapshot_active_puuid")
        if not session_key or not puuid or session_puuid != puuid:
            return 0
        cached_puuid, solo_entry = self._cached_owner_solo_entry(puuid)
        if cached_puuid != puuid or not solo_entry:
            return 0
        post = self.storage.save_rank_snapshot(
            puuid, solo_entry, stage="POST", session_key=session_key,
        )
        if post is None:
            return 0
        matches = self.storage.player_matches(puuid, limit=50)
        resolved = self.storage.resolve_match_lp_changes(puuid, matches)
        session_snapshot_ids = {
            snapshot.snapshot_id
            for snapshot in self.storage.load_rank_snapshots(puuid)
            if snapshot.session_key == session_key
        }
        changes = self.storage.load_match_lp_changes(
            str((match.get("metadata") or {}).get("matchId") or "")
            for match in matches
        )
        already_linked = any(
            change.before_snapshot_id in session_snapshot_ids
            and change.after_snapshot_id in session_snapshot_ids
            for change in changes.values()
        )
        if resolved or already_linked:
            self.storage.set_setting("rank_snapshot_active_session", "")
            self.storage.set_setting("rank_snapshot_active_puuid", "")
        return resolved

    def _cancel_post_game_sync(self) -> None:
        """Cancel a pending post-game retry chain without touching Riot data."""
        self._post_game_sync_generation += 1
        after_id = self._post_game_sync_after_id
        self._post_game_sync_after_id = None
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass

    def _begin_post_game_sync(self) -> None:
        """Refresh until Match-v5 exposes the match that just ended."""
        self._cancel_post_game_sync()
        generation = self._post_game_sync_generation
        self._post_game_sync_baseline_match_id = self.storage.get_setting(
            "riot_latest_match_id"
        ).strip()
        self._schedule_post_game_sync_attempt(generation, 0)

    def _schedule_post_game_sync_attempt(
        self, generation: int, attempt: int,
    ) -> None:
        if generation != self._post_game_sync_generation:
            return
        if attempt >= len(POST_GAME_SYNC_RETRY_DELAYS_MS):
            self._post_game_sync_after_id = None
            self.exchange_status.configure(
                text=self._text("history.postgame_delayed"), fg=COLORS["orange"]
            )
            return
        delay = POST_GAME_SYNC_RETRY_DELAYS_MS[attempt]
        self.exchange_status.configure(
            text=self._text(
                "history.postgame_wait", attempt=attempt + 1,
                total=len(POST_GAME_SYNC_RETRY_DELAYS_MS),
            ),
            fg=COLORS["blue"],
        )
        self._post_game_sync_after_id = self.root.after(
            delay,
            lambda: self._run_post_game_sync_attempt(generation, attempt),
        )

    def _run_post_game_sync_attempt(
        self, generation: int, attempt: int,
    ) -> None:
        if generation != self._post_game_sync_generation:
            return
        self._post_game_sync_after_id = None
        if self.game_phase in {"GameStart", "Reconnect", "InProgress"}:
            self._cancel_post_game_sync()
            return
        self._sync_riot(automatic=True, game_finished=True)
        self._post_game_sync_after_id = self.root.after(
            POST_GAME_SYNC_CHECK_INTERVAL_MS,
            lambda: self._check_post_game_sync_attempt(generation, attempt),
        )

    def _check_post_game_sync_attempt(
        self, generation: int, attempt: int,
    ) -> None:
        if generation != self._post_game_sync_generation:
            return
        self._post_game_sync_after_id = None
        # The sync stores the newest ID before downloading and committing the
        # full Match-v5 payload. Wait for the worker so the history screen can
        # never rebuild in the gap between those two operations.
        if self._riot_syncing:
            self._post_game_sync_after_id = self.root.after(
                POST_GAME_SYNC_CHECK_INTERVAL_MS,
                lambda: self._check_post_game_sync_attempt(generation, attempt),
            )
            return
        latest_match_id = self.storage.get_setting("riot_latest_match_id").strip()
        if (
            latest_match_id
            and latest_match_id != self._post_game_sync_baseline_match_id
        ):
            self._post_game_sync_generation += 1
            # _sync_riot normally requests this reload. Repeating force here
            # also covers a concurrently running startup sync.
            self._history_revision = None
            self._ensure_history_loaded(force=True)
            self.exchange_status.configure(
                text=self._text("history.postgame_complete"), fg=COLORS["green"]
            )
            return
        self._schedule_post_game_sync_attempt(generation, attempt + 1)

    def _sync_riot(
        self,
        automatic: bool = False,
        game_finished: bool = False,
        startup: bool = False,
        on_complete: Callable[[bool], None] | None = None,
    ) -> None:
        if self._riot_syncing:
            if on_complete:
                on_complete(False)
            return
        started_in_game = self.game_phase == "InProgress"
        remaining = self._riot_history_cooldown_remaining()
        if (
            not game_finished
            and not startup
            and remaining.total_seconds() > 0
        ):
            if not automatic:
                messagebox.showinfo(
                    "내 전적 로컬 캐시",
                    "같은 전적 요청은 1분 동안 로컬 데이터를 사용합니다. "
                    "게임이 끝나 새 경기가 생기면 자동 갱신됩니다.",
                    parent=self.root,
                )
            if on_complete:
                on_complete(True)
            return
        game_name = self.storage.get_setting("riot_game_name")
        tag_line = self.storage.get_setting("riot_tag_line")
        api_key = self.storage.get_setting("riot_api_key")
        if not api_key:
            if automatic:
                if on_complete:
                    on_complete(False)
                return
            self._open_settings()
            if on_complete:
                on_complete(False)
            return
        if self.storage.riot_api_key_needs_refresh():
            if not automatic:
                messagebox.showwarning(
                    "개발용 API 키 갱신 필요",
                    "개발용 키는 약 24시간마다 만료됩니다. 상단의 Riot 키 발급/갱신 버튼에서 "
                    "새 키를 받은 뒤 Riot 설정에 입력하세요.",
                    parent=self.root,
                )
            if on_complete:
                on_complete(False)
            return
        if not game_name or not tag_line:
            if not automatic:
                messagebox.showinfo(
                    "Riot ID 자동 감지",
                    "롤 클라이언트 연결 후 게임 이름과 태그를 자동으로 감지합니다.",
                    parent=self.root,
                )
            if on_complete:
                on_complete(False)
            return
        self._riot_syncing = True
        self.riot_button.configure(
            state="disabled",
            text=self._text(
                "history.sync.in_game_progress"
                if started_in_game else "history.sync.progress"
            ),
        )

        def progress(done: int, total: int) -> None:
            self._post_ui(lambda: self.riot_button.configure(text=f"전적 {done}/{total}"))

        def work() -> tuple[str, int, int]:
            # The owner's large initial sync and inspected-player 10-game
            # pages must not consume the same regional rate budget at once.
            with self._riot_history_request_lock:
                result = RiotApiClient(api_key).sync(
                    self.storage,
                    game_name,
                    tag_line,
                    count=1000,
                    progress=progress,
                )
            self._finalize_pending_rank_snapshot(result[0])
            return result

        def success(result: tuple[str, int, int]) -> None:
            _puuid, saved, total = result
            self.storage.mark_riot_sync()
            self.storage.mark_cache_job_success("riot_history")
            self._riot_syncing = False
            self.riot_button.configure(text="전적 갱신", state="normal")
            self.exchange_status.configure(
                text=self._text(
                    "history.sync.in_game_complete"
                    if started_in_game else "history.sync.complete",
                    saved=saved,
                    total=total,
                ),
                fg=COLORS["green"],
            )
            self._history_revision = None
            self._ensure_history_loaded(force=True)
            self._render_selection()
            if on_complete:
                on_complete(True)

        def error(exc: Exception) -> None:
            self._riot_syncing = False
            if isinstance(exc, RiotApiError) and "만료" in str(exc):
                self.storage.mark_riot_api_key_invalid()
            self.riot_button.configure(text="전적 갱신", state="normal")
            self._render_header()
            messagebox.showerror("내 전적 갱신 실패", str(exc), parent=self.root)
            if on_complete:
                on_complete(False)

        self._background(work, success, error)

    def _background(
        self, work: Callable[[], T], success: Callable[[T], None], error: Callable[[Exception], None]
    ) -> None:
        if getattr(self, "_closing", False):
            return

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # All worker failures must return to the GUI thread.
                self._post_ui(lambda captured=exc: error(captured))
            else:
                self._post_ui(lambda captured=result: success(captured))
        try:
            self._background_executor.submit(runner)
        except RuntimeError:
            # shutdown(cancel_futures=True) races safely with late pollers.
            return

    def _post_ui(self, callback: Callable[[], None]) -> None:
        if getattr(self, "_closing", False):
            return
        self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        if getattr(self, "_closing", False):
            return
        started = time.perf_counter()
        processed = 0
        try:
            while processed < 16 and time.perf_counter() - started < 0.008:
                try:
                    callback = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                try:
                    callback()
                except Exception:
                    # One stale widget callback must not permanently stop all
                    # later background results from reaching Tk.
                    continue
        finally:
            try:
                if processed:
                    self._schedule_language_refresh()
                # Yield immediately after a full slice so mouse/paint events
                # always get a turn while loaders are publishing.
                delay = (
                    1 if processed >= 16
                    or time.perf_counter() - started >= 0.008 else 80
                )
                self.root.after(delay, self._drain_ui_queue)
            except tk.TclError:
                pass

    def _refresh_registry_background(self) -> None:
        if self.registry.loaded_from_ddragon:
            return

        def success(_count: int) -> None:
            self._invalidate_selection_panels()
            self.icon_cache.prefetch_all(self._invalidate_all_champion_icon_panels)
            self._refresh_build_champion_values()
            self._render_all()

        self._background(lambda: self.registry.refresh(), success, lambda _exc: None)

    def _reset_draft_after_dodge(self) -> None:
        """Clear every champ-select-only value when a draft is dodged."""
        role = str(getattr(self.draft, "my_role", "SUPPORT") or "SUPPORT")
        self.draft = DraftSnapshot(my_role=role, connection_state="LOBBY")
        self.draft.refresh_snapshot_id()
        self._manual_enemy_support = None
        self._support_filter = "ALL"
        self._recommendation_generation += 1
        self.recommendations = []
        self.recommendation_source = ""
        self.recommendation_snapshot_id = ""
        self.recommendation_context_signature = ""
        self.recommendation_enemy_support_id = ""
        self._local_recommendation_signature = ""
        self._prompt_copied_snapshot_id = ""
        self._champ_select_inner_phase = ""
        self._local_pick_action_in_progress = False
        self.opgg_meta_snapshot = self.storage.load_opgg_snapshot(None, role)
        self.opgg_snapshot = self.opgg_meta_snapshot
        self.opgg_synergy_snapshot = None
        self._synergy_checked_adc = ""
        self._selection_matchup_refreshing.clear()
        self._selection_panel_signatures.clear()

    def _stop_matchmaking_after_dodge(self) -> None:
        if not self.stop_queue_after_dodge_enabled or self.demo:
            return

        def work() -> bool:
            # Lobby and Matchmaking can arrive in either order after a dodge.
            # Retry briefly in a worker so Tk and LCU polling remain responsive.
            for _attempt in range(18):
                if not self.stop_queue_after_dodge_enabled:
                    return False
                phase = str(self.lcu.get("/lol-gameflow/v1/gameflow-phase"))
                if phase in {"ReadyCheck", "ChampSelect", "GameStart", "InProgress"}:
                    return False
                if phase in {"Lobby", "Matchmaking", "None"}:
                    try:
                        self.lcu.stop_matchmaking_search()
                    except LcuUnavailable:
                        time.sleep(0.2)
                        continue
                    return True
                time.sleep(0.2)
            return False

        def success(stopped: bool) -> None:
            if stopped:
                self.exchange_status.configure(
                    text="닷지 감지 · 게임찾기 자동 중단 완료",
                    fg=COLORS["orange"],
                )

        self._background(work, success, lambda _exc: None)

    def _poll_lcu(self) -> None:
        if self.demo or self._lcu_polling:
            return
        self._lcu_polling = True

        need_identity = not self._identity_checked
        auto_accept_enabled = self.auto_accept_enabled
        lux_auto_ban_enabled = self.lux_auto_ban_enabled
        auto_ban_champion_key = self._auto_ban_champion()[0]

        def work() -> tuple[str, DraftSnapshot | None, dict, dict[str, object]]:
            phase = str(self.lcu.get("/lol-gameflow/v1/gameflow-phase"))
            draft = None
            automation: dict[str, object] = {}
            if phase == "ReadyCheck" and auto_accept_enabled:
                automation["auto_accept_ready"] = True
            if phase == "ChampSelect":
                session = self.lcu.champ_select_session()
                draft = parse_lcu_session(session, self.registry)
                automation["champ_select_inner_phase"] = (
                    champ_select_timer_phase(session)
                )
                try:
                    find_local_champion_action(
                        session, "pick", require_in_progress=True,
                    )
                except LcuUnavailable:
                    automation["local_pick_in_progress"] = False
                else:
                    automation["local_pick_in_progress"] = True
                if (
                    lux_auto_ban_enabled
                    and champ_select_timer_phase(session) == "BAN_PICK"
                    and auto_ban_champion_key
                    not in session_banned_champion_ids(session)
                ):
                    try:
                        lux_action = find_local_champion_action(
                            session, "ban", require_in_progress=True,
                        )
                    except LcuUnavailable:
                        pass
                    else:
                        automation["lux_action_id"] = int(
                            lux_action.get("id") or 0
                        )
                        automation["lux_session"] = session
            identity: dict = {}
            if need_identity:
                try:
                    identity = dict(self.lcu.get("/lol-summoner/v1/current-summoner"))
                except LcuUnavailable:
                    identity = {}
            return phase, draft, identity, automation

        def success(
            result: tuple[str, DraftSnapshot | None, dict, dict[str, object]]
        ) -> None:
            self._lcu_polling = False
            phase, draft, identity, automation = result
            self._champ_select_inner_phase = str(
                automation.get("champ_select_inner_phase") or ""
            )
            self._local_pick_action_in_progress = bool(
                automation.get("local_pick_in_progress")
            )
            previous_phase = self.game_phase
            phase_changed = previous_phase != phase
            draft_changed = False
            swap_state_changed = False
            active_game_phases = {"GameStart", "Reconnect", "InProgress"}
            draft_dodged = bool(
                previous_phase == "ChampSelect"
                and phase != "ChampSelect"
                and phase not in active_game_phases
            )
            if phase in active_game_phases and previous_phase not in active_game_phases:
                self._cancel_post_game_sync()
                # A restored previous board is presentation-only. Remove it
                # before the new loading/game endpoints begin filling live data.
                if self._showing_previous_play:
                    self._clear_current_play_state()
                # This is intentionally a local cache read and a tiny SQLite
                # write. It must finish before we publish InProgress so the
                # post-game Riot value cannot accidentally become the baseline.
                self._capture_pre_game_rank_snapshot(
                    force_new=previous_phase == "ChampSelect"
                )
            self.game_phase = phase
            if draft_dodged:
                self._reset_draft_after_dodge()
                if self.stop_queue_after_dodge_enabled:
                    self.root.after(250, self._stop_matchmaking_after_dodge)
            if automation.get("auto_accept_ready"):
                self._ensure_auto_accept_monitor()
            elif phase != "ReadyCheck":
                self._reset_auto_accept_cycle()
            lux_session = automation.get("lux_session")
            if isinstance(lux_session, dict):
                self._ensure_lux_auto_ban_monitor(
                    int(automation.get("lux_action_id") or 0), lux_session,
                )
            if identity:
                game_name = str(identity.get("gameName") or "").strip()
                tag_line = str(identity.get("tagLine") or "").strip()
                puuid = str(identity.get("puuid") or "").strip()
                if game_name:
                    self.storage.set_setting("riot_game_name", game_name)
                if tag_line:
                    self.storage.set_setting("riot_tag_line", tag_line)
                if puuid:
                    self.storage.set_setting("riot_puuid", puuid)
                self._identity_checked = bool(game_name and tag_line and puuid)
                if self._identity_checked:
                    self._schedule_non_owner_prune(25)
            if phase == "ChampSelect" and draft:
                if previous_phase != "ChampSelect":
                    # A newly opened draft is definitive proof that an older
                    # loading-roster record must not be reused, even in the
                    # extraordinarily unlikely case of identical champions.
                    self._clear_live_roster_identities()
                    self._manual_enemy_support = None
                role_changed = draft.my_role != self.draft.my_role
                old_pick_order = self.draft.my_pick_order
                old_local_cell = self.draft.local_player_cell_id
                old_hover = local_draft_selection(self.draft)
                old_hover_signature = (
                    old_hover.champion_id, old_hover.state
                ) if old_hover else (None, None)
                if role_changed:
                    self._manual_enemy_support = None
                    self._support_filter = "ALL"
                    self.opgg_synergy_snapshot = None
                    self._synergy_checked_adc = ""
                    self.opgg_meta_snapshot = self.storage.load_opgg_snapshot(
                        None, draft.my_role
                    )
                self._auto_select_enemy_support(draft)
                draft.refresh_snapshot_id()
                self._remember_draft_pick_context(draft)
                old_support = self.draft.selected_enemy_support_id
                draft_changed = draft.snapshot_id != self.draft.snapshot_id
                swap_state_changed = (
                    draft.pick_order_swap_state != self.draft.pick_order_swap_state
                    or draft.pick_order_swap_target_cell_id
                    != self.draft.pick_order_swap_target_cell_id
                )
                if draft_changed or swap_state_changed:
                    self.draft = draft
                if (
                    old_pick_order is not None
                    and draft.my_pick_order is not None
                    and old_pick_order != draft.my_pick_order
                    and old_local_cell != draft.local_player_cell_id
                ):
                    self._show_pick_order_change_notice(
                        old_pick_order, draft.my_pick_order,
                    )
                new_hover = local_draft_selection(draft)
                new_hover_signature = (
                    new_hover.champion_id, new_hover.state
                ) if new_hover else (None, None)
                if role_changed or new_hover_signature != old_hover_signature:
                    self._sync_build_selection_from_draft(
                        draft,
                        render=self._current_main_tab_index() == 3,
                    )
                if role_changed or draft.selected_enemy_support_id != old_support:
                    self.opgg_snapshot = self.storage.load_opgg_snapshot(
                        draft.selected_enemy_support_id, draft.my_role
                    )
                    self.root.after(60, self._sync_selected_matchup)
                if (
                    role_changed or draft.selected_enemy_support_id != old_support
                    or new_hover_signature != old_hover_signature
                ):
                    self.root.after(25, self._sync_hover_matchup)
                if previous_phase != "ChampSelect":
                    self.notebook.select(self.selection_tab)
                    self.root.after(40, self._sync_position_meta)
                if draft_changed or role_changed:
                    self.root.after(60, self._sync_ally_adc_synergy)
                if role_changed:
                    self.root.after(50, self._sync_position_meta)
            elif phase == "InProgress":
                self.draft.connection_state = "IN_GAME"
                if previous_phase != "InProgress":
                    self.notebook.select(self.play_tab)
                    self.root.after(35, self._poll_live)
            else:
                if (
                    previous_phase in active_game_phases
                    and phase not in active_game_phases
                ):
                    self._capture_previous_play_state()
                    self._clear_current_play_state()
                if phase not in {"GameStart", "Reconnect"}:
                    self.draft.connection_state = "LOBBY"
            if phase in {"GameStart", "Reconnect", "InProgress"}:
                # Privacy mode can expose the ten Riot IDs for only a moment on
                # the loading screen. A tiny player-list watcher captures that
                # local response independently of Tk rendering and remote APIs.
                self._start_live_identity_capture()
            if phase_changed:
                self._render_all()
            elif draft_changed or swap_state_changed:
                self._render_selection()
            else:
                self._render_header()
            if identity and self.storage.get_setting("riot_api_key"):
                self.root.after(
                    300,
                    lambda: self._sync_riot(automatic=True, startup=True),
                )
            if (
                previous_phase in active_game_phases
                and phase not in active_game_phases
            ):
                self._begin_post_game_sync()
            # HOVER changes are local-client events and should feel immediate.
            # Poll more quickly only during champion select; external OP.GG/Riot
            # requests still keep their independent user-configured cache rules.
            pending_swap = bool(
                phase == "ChampSelect" and draft
                and draft.pick_order_swap_state in {"SENT", "RECEIVED", "ACCEPTED"}
            )
            self.root.after(
                (
                    140 if pending_swap else
                    220 if phase == "ReadyCheck" else
                    350 if phase == "ChampSelect" else 1400
                ),
                self._poll_lcu,
            )

        def error(_exc: Exception) -> None:
            self._lcu_polling = False
            if (
                self.game_phase == "ChampSelect"
                or self.draft.connection_state == "CHAMP_SELECT"
            ):
                # A brief LCU gap is common while swaps and phase transitions are
                # committed. Keep the last valid draft visible instead of flashing
                # an empty screen, then retry at champ-select speed.
                self.game_phase = "ChampSelect"
                self.root.after(350, self._poll_lcu)
                return
            self.game_phase = "None"
            if self.draft.connection_state not in {"DISCONNECTED", "IN_GAME"}:
                self.draft = DraftSnapshot()
                self._manual_enemy_support = None
                self._support_filter = "ALL"
                self.opgg_meta_snapshot = self.storage.load_opgg_snapshot(
                    None, self.draft.my_role
                )
                self.opgg_snapshot = self.opgg_meta_snapshot
                self.opgg_synergy_snapshot = None
                self._synergy_checked_adc = ""
                self.build_guide = self.storage.load_build_guide(
                    self._build_selected_champion_id, self.draft.my_role
                )
                self._build_rune_index = 0
                self._build_spell_index = 0
                if self.build_guide:
                    self._prefetch_build_assets(self.build_guide)
                self._render_all()
            self.root.after(2400, self._poll_lcu)

        self._background(work, success, error)

    def _clear_live_roster_identities(self) -> None:
        with self._live_identity_lock:
            self._live_identity_generation += 1
            self._live_identity_payload = None
            self._live_identity_resolution_auth_failed = False
            self.storage.set_setting(LIVE_IDENTITY_CACHE_SETTING, "")

    def _remember_live_roster_identities(
        self,
        snapshot: LiveGameSnapshot,
        generation: int | None = None,
    ) -> int:
        """Persist only Riot IDs actually observed from the current roster."""
        with self._live_identity_lock:
            if (
                generation is not None
                and generation != self._live_identity_generation
            ):
                return 0
            previous = self._live_identity_payload
            updated = update_live_identity_payload(snapshot, previous)
            if updated is not previous:
                self._live_identity_payload = updated
                if updated is not None:
                    self.storage.set_setting(
                        LIVE_IDENTITY_CACHE_SETTING,
                        json.dumps(updated, ensure_ascii=False, separators=(",", ":")),
                    )
            if (
                not isinstance(updated, dict)
                or str(updated.get("fingerprint") or "")
                != live_roster_fingerprint(snapshot)
            ):
                return 0
            return len(updated.get("players") or [])

    def _restore_live_roster_identities(
        self, snapshot: LiveGameSnapshot,
    ) -> LiveGameSnapshot:
        with self._live_identity_lock:
            payload = self._live_identity_payload
        return merge_live_roster_identities(snapshot, payload)

    def _prewarm_gameflow_identities(self, generation: int) -> tuple[int, int]:
        """Resolve the loading roster before Live Client playerlist is ready.

        The playerlist endpoint becomes available several seconds after the
        game process starts, which is later than Riot's brief visible-name
        window. Gameflow team rows are available earlier and retain local
        summoner ids, so warm the identity cache from those rows first.
        """
        with self._live_identity_lock:
            if generation != self._live_identity_generation:
                return 0, 0
        try:
            session = self.lcu.get("/lol-gameflow/v1/session")
        except LcuUnavailable:
            return 0, 0
        session = session if isinstance(session, dict) else {}
        summoner_ids = gameflow_summoner_id_by_champion(session)
        local_puuids = gameflow_puuid_by_champion(session)
        if not summoner_ids:
            return 0, 0

        cached = 0
        missing: list[tuple[int, str, str]] = []
        for champion_key, summoner_id in summoner_ids.items():
            local_puuid = local_puuids.get(champion_key, "")
            if local_puuid and self.storage.find_riot_id_by_puuid(local_puuid):
                cached += 1
            else:
                missing.append((champion_key, summoner_id, local_puuid))

        def resolve_one(
            target: tuple[int, str, str],
        ) -> tuple[str, str, str] | None:
            _champion_key, summoner_id, local_puuid = target
            account = self.lcu.get(
                f"/lol-summoner/v1/summoners/{summoner_id}"
            )
            if not isinstance(account, dict):
                return None
            game_name = str(account.get("gameName") or "").strip()
            tag_line = str(account.get("tagLine") or "").strip()
            if not game_name or not tag_line:
                return None
            puuid = str(account.get("puuid") or local_puuid).strip()
            return f"{game_name}#{tag_line}", puuid, summoner_id

        resolved_count = 0
        errors = 0
        if missing:
            with ThreadPoolExecutor(max_workers=min(3, len(missing))) as executor:
                futures = [executor.submit(resolve_one, target) for target in missing]
                for future in as_completed(futures):
                    try:
                        resolved = future.result()
                    except (LcuUnavailable, ValueError, TypeError):
                        errors += 1
                        continue
                    if not resolved:
                        errors += 1
                        continue
                    riot_id, puuid, _summoner_id = resolved
                    if puuid:
                        self.storage.save_player_identity(riot_id, puuid)
                        resolved_count += 1
        known = cached + resolved_count
        self._audit_live_identity(
            "gameflow_prewarm",
            total=len(summoner_ids),
            cached=cached,
            resolved=resolved_count,
            errors=errors,
        )
        return known, len(summoner_ids)

    def _resolve_private_live_identities(
        self,
        snapshot: LiveGameSnapshot,
        generation: int,
    ) -> LiveGameSnapshot:
        """Resolve redacted loading-screen names through the local client.

        Privacy-mode gameflow PUUIDs are anonymous UUIDs and Account-v1
        rejects them.  The same session retains a local summoner id for each
        champion, however, and the local summoner endpoint resolves that id to
        a Riot ID without an external API key.  This runs on the dedicated
        loading watcher, never on the Tk thread.
        """
        if live_identity_count(snapshot) >= len(snapshot.players):
            return snapshot
        with self._live_identity_lock:
            if generation != self._live_identity_generation:
                return snapshot
        try:
            session = self.lcu.get("/lol-gameflow/v1/session")
        except LcuUnavailable:
            return snapshot
        puuid_by_key = gameflow_puuid_by_champion(
            session if isinstance(session, dict) else {}
        )
        summoner_id_by_key = gameflow_summoner_id_by_champion(
            session if isinstance(session, dict) else {}
        )
        if not summoner_id_by_key:
            return snapshot

        targets: list[tuple[LivePlayer, str, str]] = []
        for player in snapshot.players:
            if live_identity_available(player):
                continue
            champion_row = self.registry.by_id.get(player.champion_id)
            if not champion_row:
                continue
            champion_key = int(champion_row[0])
            summoner_id = summoner_id_by_key.get(champion_key, "")
            if summoner_id:
                targets.append((
                    player, summoner_id, puuid_by_key.get(champion_key, ""),
                ))
        if not targets:
            return snapshot

        identities: dict[str, tuple[str, str]] = {}
        missing: list[tuple[LivePlayer, str, str]] = []
        for player, summoner_id, local_puuid in targets:
            cached_riot_id = (
                self.storage.find_riot_id_by_puuid(local_puuid)
                if local_puuid else ""
            )
            cached_parts = split_riot_id(cached_riot_id)
            if cached_parts:
                identities[player.champion_id] = cached_parts
            else:
                missing.append((player, summoner_id, local_puuid))

        def resolve_one(
            target: tuple[LivePlayer, str, str],
        ) -> tuple[str, str, str, str] | None:
            player, summoner_id, local_puuid = target
            account = self.lcu.get(
                f"/lol-summoner/v1/summoners/{summoner_id}"
            )
            if not isinstance(account, dict):
                return None
            game_name = str(account.get("gameName") or "").strip()
            tag_line = str(account.get("tagLine") or "").strip()
            if not game_name or not tag_line:
                return None
            resolved_puuid = str(account.get("puuid") or local_puuid).strip()
            return player.champion_id, resolved_puuid, game_name, tag_line

        errors = 0
        if missing:
            with ThreadPoolExecutor(max_workers=min(3, len(missing))) as executor:
                futures = [executor.submit(resolve_one, target) for target in missing]
                for future in as_completed(futures):
                    try:
                        resolved = future.result()
                    except (LcuUnavailable, ValueError, TypeError):
                        errors += 1
                        continue
                    if not resolved:
                        errors += 1
                        continue
                    champion_id, puuid, game_name, tag_line = resolved
                    identities[champion_id] = (game_name, tag_line)
                    if puuid:
                        self.storage.save_player_identity(
                            f"{game_name}#{tag_line}", puuid,
                        )
        self._audit_live_identity(
            "local_resolve",
            player_count=len(snapshot.players),
            requested=len(targets),
            cache_hits=len(targets) - len(missing),
            resolved=len(identities),
            errors=errors,
        )
        if errors and not identities:
            self._post_ui(
                lambda: self.live_profile_status.configure(
                    text=self._text(
                        "play.identity_api_error",
                        error="롤 클라이언트 신원 조회 실패",
                    ),
                    fg=COLORS["orange"],
                )
            )
        if not identities:
            return snapshot

        owner_puuid = self.storage.get_setting("riot_puuid").strip()
        owner_riot_id = (
            f"{self.storage.get_setting('riot_game_name')}#"
            f"{self.storage.get_setting('riot_tag_line')}"
        ).strip("#").casefold()
        players: list[LivePlayer] = []
        active_riot_id = snapshot.active_riot_id
        for player in snapshot.players:
            identity = identities.get(player.champion_id)
            champion_row = self.registry.by_id.get(player.champion_id)
            puuid = (
                puuid_by_key.get(int(champion_row[0]), "")
                if champion_row else ""
            )
            restored = (
                replace(
                    player,
                    riot_game_name=identity[0],
                    riot_tag_line=identity[1],
                    is_active_player=(
                        player.is_active_player
                        or bool(owner_puuid and puuid == owner_puuid)
                        or bool(
                            owner_riot_id
                            and f"{identity[0]}#{identity[1]}".casefold()
                            == owner_riot_id
                        )
                    ),
                )
                if identity else player
            )
            if restored.is_active_player and live_identity_available(restored):
                active_riot_id = restored.riot_id
            players.append(restored)
        return replace(snapshot, players=players, active_riot_id=active_riot_id)

    def _start_live_identity_capture(self) -> None:
        """Catch the short loading-screen identity window off the Tk thread."""
        if self.demo:
            return
        with self._live_identity_capture_lock:
            if self._live_identity_capture_running:
                return
            self._live_identity_capture_running = True
        with self._live_identity_lock:
            capture_generation = self._live_identity_generation

        def runner() -> None:
            self._audit_live_identity(
                "capture_started", generation=capture_generation,
            )
            started_at = time.monotonic()
            first_success_at: float | None = None
            last_fingerprint = ""
            last_known = 0
            inactive_since: float | None = None
            next_puuid_attempt_at = 0.0
            next_prewarm_attempt_at = 0.0
            prewarm_complete = False
            try:
                while time.monotonic() - started_at < LIVE_IDENTITY_CAPTURE_MAX_SECONDS:
                    with self._live_identity_lock:
                        if capture_generation != self._live_identity_generation:
                            break
                    now = time.monotonic()
                    if self.game_phase not in {"GameStart", "Reconnect", "InProgress"}:
                        inactive_since = inactive_since or now
                        if now - inactive_since >= 2.0:
                            break
                    else:
                        inactive_since = None
                    if not prewarm_complete and now >= next_prewarm_attempt_at:
                        next_prewarm_attempt_at = now + 2.0
                        prewarm_known, prewarm_total = (
                            self._prewarm_gameflow_identities(capture_generation)
                        )
                        prewarm_complete = bool(
                            prewarm_total >= 10 and prewarm_known >= prewarm_total
                        )
                    try:
                        snapshot = self.live_client.identity_snapshot()
                    except LiveClientUnavailable:
                        time.sleep(0.25)
                        continue
                    fingerprint = live_roster_fingerprint(snapshot)
                    if fingerprint != last_fingerprint:
                        first_success_at = now
                        last_fingerprint = fingerprint
                    elif first_success_at is None:
                        first_success_at = now
                    known = self._remember_live_roster_identities(
                        snapshot, capture_generation,
                    )
                    player_count = len(snapshot.players)
                    if (
                        player_count >= 10 and known < player_count
                        and now >= next_puuid_attempt_at
                    ):
                        next_puuid_attempt_at = now + 3.0
                        snapshot = self._resolve_private_live_identities(
                            snapshot, capture_generation,
                        )
                        known = self._remember_live_roster_identities(
                            snapshot, capture_generation,
                        )
                    if known > last_known:
                        self._audit_live_identity(
                            "capture_progress", known=known, total=player_count,
                        )
                        last_known = known
                        self._post_ui(self._poll_live)
                    if player_count and known >= player_count:
                        break
                    if first_success_at is not None and now - first_success_at >= LIVE_IDENTITY_CAPTURE_FAST_SECONDS:
                        break
                    time.sleep(0.10)
            finally:
                self._audit_live_identity(
                    "capture_finished", known=last_known,
                    elapsed=round(time.monotonic() - started_at, 3),
                )
                with self._live_identity_capture_lock:
                    self._live_identity_capture_running = False

        threading.Thread(
            target=runner, name="live-identity-capture", daemon=True,
        ).start()

    def _poll_live(self) -> None:
        if self.demo or self.game_phase != "InProgress" or self._live_polling:
            return
        self._live_polling = True

        def work() -> LiveGameSnapshot:
            snapshot = self.live_client.snapshot()
            self._remember_live_roster_identities(snapshot)
            return snapshot

        def success(snapshot: LiveGameSnapshot) -> None:
            self._live_polling = False
            snapshot = self._restore_live_roster_identities(snapshot)
            self._attach_draft_pick_context(snapshot)
            signature = live_roster_signature(snapshot)
            active_signature = live_active_context_signature(snapshot)
            roster_changed = signature != self._live_signature
            active_changed = active_signature != self._live_active_signature
            self.live_game = snapshot
            if roster_changed:
                self._live_signature = signature
                self._live_active_signature = active_signature
                self.duo_pairs = {}
                self._duo_checked_signature = ""
                self.opgg_player_profiles = {}
                self._opgg_profiles_checked_signature = ""
                self._opgg_profile_failures = 0
                self.player_profiles = {
                    player.riot_id: PlayerProfileStat(status="LOADING") for player in snapshot.players
                }
                self.player_behaviors = {}
                self.jungle_tendencies = {}
                self._jungle_tendency_context = None
                self._lane_opponent_analysis_context = None
                self._lane_opponent_personal_stat = None
                self._lane_opponent_behavior = None
                self._my_account_analysis_context = None
                self._my_personal_stat = None
                self._my_behavior = None
                self._play_insight_signature = ""
                self._prepare_live_lane_matchups(snapshot, signature)
                # My own PUUID is persisted locally, so coaching can start before
                # the slower ten-player profile pass completes.
                self.root.after(20, self._ensure_my_account_analysis)
                self._load_live_profiles()
                self._load_opgg_live_profiles()
                self._check_live_duos()
            elif active_changed:
                # `/activeplayer` can briefly be empty at game start while the
                # roster endpoint is already complete.  Patch the perspective
                # and coaching sections without throwing away ten profiles or
                # making the same remote requests again.
                self._live_active_signature = active_signature
                self._lane_opponent_analysis_context = None
                self._lane_opponent_personal_stat = None
                self._lane_opponent_behavior = None
                self._my_account_analysis_context = None
                self._my_personal_stat = None
                self._my_behavior = None
                self._play_insight_signature = ""
                self.root.after(15, self._ensure_lane_opponent_analysis)
                self.root.after(20, self._ensure_my_account_analysis)
            if roster_changed or active_changed:
                self._render_play()
            else:
                # The roster and every analysis input are unchanged.  Only the
                # game clock needs a cheap update on the steady 3-second poll.
                self._render_live_game_clock()
            known_identities = live_identity_count(snapshot)
            retry_delay = (
                140 if snapshot.game_time <= 5.0
                and known_identities < len(snapshot.players)
                else 3000
            )
            self.root.after(retry_delay, self._poll_live)

        def error(_exc: Exception) -> None:
            self._live_polling = False
            # The first Live Client endpoint can appear partway through the
            # loading screen. Retry quickly before the brief Riot-ID window is
            # redacted; steady-state failures retain the old, slower cadence.
            self.root.after(140 if not self.live_game.players else 1800, self._poll_live)

        self._background(work, success, error)

    def _profile_with_opgg(
        self,
        base: PlayerProfileStat,
        opgg_profile: OpggMcpSummonerProfile,
        player: LivePlayer,
    ) -> PlayerProfileStat:
        """Overlay OP.GG season data without discarding Riot/local relationships."""
        if opgg_profile.status == "PRIVATE_OR_UNAVAILABLE":
            if base.status in {"OK", "LOCAL_ONLY", "PARTIAL"}:
                return replace(
                    base,
                    champion_source_detail="OP.GG 비공개/조회 불가 · Riot 캐시 사용",
                )
            return replace(
                base,
                champion_data_source="PRIVATE_OR_UNAVAILABLE",
                sample_scope="계정 비공개 또는 조회 불가",
                status="PRIVATE_OR_UNAVAILABLE",
            )
        tier = base.tier
        rank = base.rank
        league_points = base.league_points
        if (
            not player.is_active_player
            and opgg_profile.tier != "UNRANKED"
        ) or tier == "UNRANKED":
            tier = opgg_profile.tier
            rank = opgg_profile.division
            league_points = opgg_profile.league_points
        season_wins = base.season_wins
        season_losses = base.season_losses
        if (
            opgg_profile.season_wins + opgg_profile.season_losses
            and (
                not player.is_active_player
                or base.season_wins + base.season_losses == 0
            )
        ):
            season_wins = opgg_profile.season_wins
            season_losses = opgg_profile.season_losses

        champion_key = int(self.registry.by_id.get(player.champion_id, (0, ""))[0])

        values: dict = {
            "tier": tier,
            "rank": rank,
            "league_points": league_points,
            "season_wins": season_wins,
            "season_losses": season_losses,
            "updated_at": (
                base.updated_at
                if player.is_active_player and base.updated_at
                else opgg_profile.fetched_at or base.updated_at
            ),
            # OP.GG may legitimately report an unranked account.  That must
            # not turn a completed local relationship scan back into the
            # PARTIAL/loading state forever.
            "status": (
                "OK" if tier != "UNRANKED" or season_wins + season_losses
                else base.status if base.status in {"OK", "LOCAL_ONLY"}
                else "PARTIAL"
            ),
        }
        if (
            opgg_profile.recent_matches_status in {"OK", "EMPTY"}
            and not player.is_active_player
            and base.recent_form_source != "RIOT_LOCAL"
        ):
            recent_form = opgg_recent_form(opgg_profile, champion_key)
            last_game_champion_key = int(
                recent_form.pop("last_game_champion_key", 0) or 0
            )
            recent_form["last_game_champion_id"] = (
                self.registry.by_key.get(
                    last_game_champion_key,
                    (f"Champion{last_game_champion_key}", ""),
                )[0]
                if last_game_champion_key > 0 else ""
            )
            values.update(recent_form)
            values["recent_form_source"] = "OPGG"
        if not player.is_active_player:
            champion_stat = opgg_profile.champion_stat(champion_key)
            if champion_stat:
                values.update({
                    "champion_games": champion_stat.games,
                    "champion_wins": champion_stat.wins,
                    "local_sample_games": champion_stat.games,
                    "champion_data_source": "OPGG",
                    "champion_sample_target": champion_stat.games,
                    "champion_source_detail": (
                        opgg_profile.source_updated_at or opgg_profile.fetched_at
                    ),
                    "sample_scope": f"OP.GG 시즌 {champion_stat.games}경기",
                })
            else:
                # ranked_most_champions is a bounded champion pool. Absence is
                # not proof of zero games, so never display a fabricated 0%.
                values.update({
                    "champion_games": 0,
                    "champion_wins": 0,
                    "local_sample_games": 0,
                    "champion_data_source": "OPGG_NOT_LISTED",
                    "champion_sample_target": 0,
                    "champion_source_detail": (
                        opgg_profile.source_updated_at or opgg_profile.fetched_at
                    ),
                    "sample_scope": "OP.GG 시즌 상위 챔피언 목록 밖",
                })
        return replace(base, **values)

    def _apply_opgg_live_profile(
        self,
        riot_id: str,
        opgg_profile: OpggMcpSummonerProfile,
        signature: str,
    ) -> None:
        if signature != self._live_signature:
            return
        player = next(
            (item for item in self.live_game.players if item.riot_id == riot_id),
            None,
        )
        if not player:
            return
        self.opgg_player_profiles[riot_id] = opgg_profile
        base = self.player_profiles.get(riot_id, PlayerProfileStat(status="LOADING"))
        merged = self._profile_with_opgg(base, opgg_profile, player)
        previous = self.player_profiles.get(riot_id)
        self.player_profiles[riot_id] = merged
        if self._comparable_live_position(player.position) == "JUNGLE":
            # OP.GG recent matches arrive after the fast player card. Re-run
            # only the jungle insight section so a local-cache miss can be
            # replaced by the remote summary without rebuilding the board.
            self._jungle_tendency_context = None
            self.root.after(10, self._ensure_jungle_tendencies)
        if (
            self._player_profile_render_value(previous)
            == self._player_profile_render_value(merged)
        ):
            return
        self._schedule_play_render()

    def _load_opgg_live_profiles(self) -> None:
        if self.demo or not self.live_game.players:
            return
        if self._opgg_profiles_loading:
            self._opgg_profile_reload_requested = True
            return
        if self._opgg_profiles_checked_signature == self._live_signature:
            return
        self._opgg_profiles_loading = True
        self._opgg_profile_reload_requested = False
        signature = self._live_signature
        players = [
            player for player in self.live_game.players
            if live_identity_available(player)
        ]
        if not players:
            self._opgg_profiles_loading = False
            self._opgg_profiles_checked_signature = self._live_signature
            self.live_profile_status.configure(
                text="로딩 구간에서 Riot ID를 확인하지 못했습니다 · 비공개 표시 유지",
                fg=COLORS["muted"],
            )
            return

        def work() -> tuple[int, int, int, str]:
            cached_count = refreshed_count = failure_count = 0
            last_error = ""
            refresh_players: list[LivePlayer] = []
            cached_riot_ids: set[str] = set()
            for player in players:
                fresh = self.storage.load_opgg_player_profile(
                    player.riot_id,
                    max_age=self._request_max_age(
                        "player_analysis_cooldown_hours"
                    ),
                )
                cached = fresh or self.storage.load_opgg_player_profile_any_age(
                    player.riot_id
                )
                if cached:
                    cached_count += 1
                    cached_riot_ids.add(player.riot_id)
                    self._post_ui(
                        lambda riot_id=player.riot_id, profile=cached:
                        self._apply_opgg_live_profile(riot_id, profile, signature)
                    )
                if not fresh and player.riot_game_name and player.riot_tag_line:
                    refresh_players.append(player)

            def fetch(player: LivePlayer) -> tuple[LivePlayer, OpggMcpSummonerProfile]:
                client = OpggMcpClient(timeout=15.0)
                profile = client.summoner_profile(
                    player.riot_game_name, player.riot_tag_line, region="KR", lang="ko_KR"
                )
                try:
                    profile.recent_matches = client.summoner_recent_matches(
                        player.riot_game_name, player.riot_tag_line,
                        region="KR", lang="ko_KR", limit=10,
                    )
                    profile.recent_matches_status = (
                        "OK" if profile.recent_matches else "EMPTY"
                    )
                except OpggMcpError:
                    # Rank/champion season data remains useful even when the
                    # separate recent-match tool is temporarily unavailable.
                    profile.recent_matches_status = "ERROR"
                return player, profile

            if refresh_players:
                worker_count = min(3, len(refresh_players))
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = {executor.submit(fetch, player): player for player in refresh_players}
                    for future in as_completed(futures):
                        player = futures[future]
                        try:
                            _player, profile = future.result()
                            self.storage.save_opgg_player_profile(profile)
                            refreshed_count += 1
                            self._post_ui(
                                lambda riot_id=player.riot_id, value=profile:
                                self._apply_opgg_live_profile(riot_id, value, signature)
                            )
                            self._post_ui(
                                lambda done=refreshed_count, total=len(refresh_players):
                                self.live_profile_status.configure(
                                    text=f"OP.GG MCP 시즌 전적 {done}/{total}명 갱신 중",
                                    fg=COLORS["blue"],
                                )
                            )
                        except (OpggMcpError, ValueError, TypeError) as exc:
                            failure_count += 1
                            last_error = str(exc)
                            if (
                                player.riot_id not in cached_riot_ids
                                and opgg_account_unavailable_error(exc)
                            ):
                                unavailable = OpggMcpSummonerProfile(
                                    riot_id=player.riot_id,
                                    game_name=player.riot_game_name,
                                    tag_line=player.riot_tag_line,
                                    region="KR",
                                    fetched_at=datetime.now().isoformat(timespec="seconds"),
                                    recent_matches_status="ERROR",
                                    status="PRIVATE_OR_UNAVAILABLE",
                                )
                                self.storage.save_opgg_player_profile(unavailable)
                                self._post_ui(
                                    lambda riot_id=player.riot_id, value=unavailable:
                                    self._apply_opgg_live_profile(
                                        riot_id, value, signature,
                                    )
                                )
            return cached_count, refreshed_count, failure_count, last_error

        def success(result: tuple[int, int, int, str]) -> None:
            self._opgg_profiles_loading = False
            cached_count, refreshed_count, failure_count, last_error = result
            if signature != self._live_signature:
                self.root.after(60, self._load_opgg_live_profiles)
                return
            self._opgg_profiles_checked_signature = signature
            self._opgg_profile_failures = failure_count
            ready = len(self.opgg_player_profiles)
            if failure_count:
                self.live_profile_status.configure(
                    text=(
                        f"OP.GG MCP {ready}/{len(players)}명 · 실패 {failure_count}명 · "
                        f"캐시 {cached_count}명"
                    ),
                    fg=COLORS["orange"],
                )
            else:
                self.live_profile_status.configure(
                    text=(
                        f"OP.GG MCP {ready}/{len(players)}명 · "
                        f"신규 {refreshed_count} / 캐시 {cached_count}"
                    ),
                    fg=COLORS["green"],
                )
            self._schedule_play_render()
            if self._opgg_profile_reload_requested:
                self._opgg_profile_reload_requested = False
                self._opgg_profiles_checked_signature = ""
                self.root.after(60, self._load_opgg_live_profiles)

        def error(exc: Exception) -> None:
            self._opgg_profiles_loading = False
            if signature == self._live_signature:
                self.live_profile_status.configure(
                    text=f"OP.GG MCP 갱신 중단 · {exc}", fg=COLORS["red"]
                )

        self._background(work, success, error)

    def _load_live_profiles(self) -> None:
        if self._profiles_loading:
            self._profile_reload_requested = True
            return
        if not self.live_game.players:
            return
        self._profiles_loading = True
        self._profile_reload_requested = False
        signature = self._live_signature
        players = sorted(
            self.live_game.players,
            key=lambda player: (
                not player.is_active_player,
                player.team != self.live_game.active_team,
                player.position,
            ),
        )

        def work() -> None:
            my_puuid = self.storage.get_setting("riot_puuid")
            cached_rows: list[tuple[LivePlayer, str, dict, str]] = []
            # Phase 1: one cheap cache lookup per player. These partial profiles
            # are posted before any match-history scans begin.
            for player in players:
                if not live_identity_available(player):
                    cached_rows.append((player, "", {}, ""))
                    self._post_ui(
                        lambda riot_id=player.riot_id:
                        self._apply_live_profile(
                            riot_id, unavailable_player_profile(), signature,
                        )
                    )
                    continue
                cached = self.storage.load_live_profile_any_age(player.riot_id)
                if cached:
                    puuid, payload, updated_at = cached
                else:
                    puuid = self.storage.find_puuid_by_riot_id(player.riot_id)
                    payload = {}
                    updated_at = ""
                if player.is_active_player and puuid:
                    my_puuid = puuid
                cached_rows.append((player, puuid, payload, updated_at))
                partial = (
                    self._make_cached_player_profile(
                        puuid,
                        payload,
                        updated_at,
                        player.champion_id,
                        player.is_active_player,
                        self._request_max_age("player_analysis_cooldown_hours"),
                    )
                    if puuid else PlayerProfileStat(status="NO_LOCAL_DATA")
                )
                self._post_ui(
                    lambda riot_id=player.riot_id, profile=partial:
                    self._apply_live_profile(riot_id, profile, signature)
                )

            # Phase 2: local champion, last-game and relationship scans. Publish
            # each player as soon as it is ready instead of waiting for all ten.
            for player, puuid, payload, updated_at in cached_rows:
                if not puuid:
                    continue
                profile = self._make_player_profile(
                    player, puuid, payload, my_puuid, updated_at
                )
                behavior = self.storage.player_behavior(
                    puuid,
                    player.champion_id,
                    position=self._comparable_live_position(player.position),
                    limit=20,
                )
                self._post_ui(
                    lambda riot_id=player.riot_id, completed=profile:
                    self._apply_live_profile(riot_id, completed, signature)
                )
                self._post_ui(
                    lambda riot_id=player.riot_id, completed=behavior:
                    self._apply_live_player_behavior(
                        riot_id, completed, signature,
                    )
                )

        def success(_result: None) -> None:
            self._profiles_loading = False
            if signature == self._live_signature:
                self._schedule_play_render()
                if self._profile_reload_requested:
                    self._profile_reload_requested = False
                    self.root.after(60, self._load_live_profiles)

        def error(exc: Exception) -> None:
            self._profiles_loading = False
            self.live_profile_status.configure(text=str(exc), fg=COLORS["red"])
            if self._profile_reload_requested and signature == self._live_signature:
                self._profile_reload_requested = False
                self.root.after(500, self._load_live_profiles)

        self._background(work, success, error)

    @staticmethod
    def _make_cached_player_profile(
        puuid: str,
        payload: dict,
        updated_at: str,
        champion_id: str = "",
        is_active_player: bool = False,
        max_age: timedelta = timedelta(hours=24),
    ) -> PlayerProfileStat:
        entry = payload.get("solo_entry") or {}
        opgg_stat = (
            None if is_active_player or not champion_id
            else opgg_champion_stat_from_payload(
                payload, champion_id, max_age=max_age
            )
        )
        remote_sample = (
            None if opgg_stat or is_active_player or not champion_id
            else live_champion_sample_from_payload(
                payload, champion_id, max_age=max_age
            )
        )
        if opgg_stat:
            opgg_wins, opgg_losses, source_detail = opgg_stat
            inspected = champion_games = target = opgg_wins + opgg_losses
            champion_wins = opgg_wins
            champion_source = "OPGG"
        else:
            inspected, champion_games, champion_wins, target = (
                remote_sample or (0, 0, 0, 0)
            )
            source_detail = ""
            champion_source = "RIOT_LIVE" if remote_sample else "RIOT_PENDING"
        return PlayerProfileStat(
            puuid=puuid,
            tier=str(entry.get("tier") or "UNRANKED"),
            rank=str(entry.get("rank") or ""),
            league_points=int(entry.get("leaguePoints") or 0),
            season_wins=int(entry.get("wins") or 0),
            season_losses=int(entry.get("losses") or 0),
            champion_games=champion_games,
            champion_wins=champion_wins,
            local_sample_games=inspected,
            champion_data_source=(
                "LOCAL" if is_active_player else
                champion_source
            ),
            champion_sample_target=target,
            champion_source_detail=source_detail,
            sample_scope="기본 캐시 · 상세 계산 중",
            updated_at=updated_at,
            status="PARTIAL",
        )

    def _make_player_profile(
        self,
        player: LivePlayer,
        puuid: str,
        payload: dict,
        my_puuid: str,
        updated_at: str,
    ) -> PlayerProfileStat:
        entry = payload.get("solo_entry") or {}
        max_age = self._request_max_age("player_analysis_cooldown_hours")
        opgg_stat = (
            None if player.is_active_player
            else opgg_champion_stat_from_payload(
                payload, player.champion_id, max_age=max_age
            )
        )
        remote_sample = (
            None if opgg_stat or player.is_active_player
            else live_champion_sample_from_payload(
                payload, player.champion_id, max_age=max_age
            )
        )
        if opgg_stat:
            champion_wins, champion_losses, champion_source_detail = opgg_stat
            champion_games = champion_wins + champion_losses
            sample_games = sample_target = champion_games
            champion_data_source = "OPGG"
        elif remote_sample:
            sample_games, champion_games, champion_wins, sample_target = remote_sample
            champion_data_source = "RIOT_LIVE"
            champion_source_detail = ""
        elif player.is_active_player:
            sample_games = self.storage.count_player_matches(puuid, limit=1000)
            champion_games, champion_wins = self.storage.player_champion_record(
                puuid, player.champion_id, limit=1000
            )
            sample_target = sample_games
            champion_data_source = "LOCAL"
            champion_source_detail = ""
        else:
            # Other players must never look like a trustworthy live value merely
            # because an old match happened to be present in our local database.
            sample_games = champion_games = champion_wins = sample_target = 0
            champion_data_source = "RIOT_PENDING"
            champion_source_detail = ""
        last_game = (
            self.storage.latest_player_match(puuid) or {}
            if player.is_active_player or champion_data_source == "RIOT_LIVE"
            else {}
        )
        if player.is_active_player:
            local_recent_matches = self.storage.player_matches(puuid, limit=10)
        else:
            # Match-v5 is authoritative for the newest result. OP.GG can lag
            # one game behind even when its profile cache was fetched moments
            # ago, so prefer the contiguous cached prefix of Riot's newest IDs.
            local_recent_matches = []
            recent_ids = recent_match_ids_from_payload(
                payload, max_age=max_age,
            ) or []
            for match_id in recent_ids[:10]:
                cached_match = self.storage.load_match(match_id)
                if cached_match is None:
                    break
                local_recent_matches.append(cached_match)
        local_recent = (
            riot_local_recent_form(
                local_recent_matches, puuid, player.champion_id,
            )
            if local_recent_matches else {}
        )
        has_local_recent = bool(local_recent.get("recent_games"))
        relationship: dict = {}
        if my_puuid and my_puuid != puuid:
            relationship = self.storage.relationship_summary(my_puuid, puuid, limit=1000)
        return PlayerProfileStat(
            puuid=puuid,
            tier=str(entry.get("tier") or "UNRANKED"),
            rank=str(entry.get("rank") or ""),
            league_points=int(entry.get("leaguePoints") or 0),
            season_wins=int(entry.get("wins") or 0),
            season_losses=int(entry.get("losses") or 0),
            champion_games=champion_games,
            champion_wins=champion_wins,
            local_sample_games=sample_games,
            champion_data_source=champion_data_source,
            champion_sample_target=sample_target,
            champion_source_detail=champion_source_detail,
            recent_games=int(local_recent.get("recent_games", 0) or 0),
            recent_wins=int(local_recent.get("recent_wins", 0) or 0),
            recent_kills=int(local_recent.get("recent_kills", 0) or 0),
            recent_deaths=int(local_recent.get("recent_deaths", 0) or 0),
            recent_assists=int(local_recent.get("recent_assists", 0) or 0),
            overall_streak=int(local_recent.get("overall_streak", 0) or 0),
            champion_recent_games=int(
                local_recent.get("champion_recent_games", 0) or 0
            ),
            champion_recent_wins=int(
                local_recent.get("champion_recent_wins", 0) or 0
            ),
            champion_streak=int(local_recent.get("champion_streak", 0) or 0),
            recent_form_source=("RIOT_LOCAL" if has_local_recent else ""),
            together_games=int(relationship.get("together_games", 0)),
            together_wins=int(relationship.get("together_wins", 0)),
            against_games=int(relationship.get("against_games", 0)),
            against_my_wins=int(relationship.get("against_my_wins", 0)),
            recent_10_together_games=int(relationship.get("recent_10_together_games", 0)),
            recent_10_against_games=int(relationship.get("recent_10_against_games", 0)),
            last_met_game_number=int(relationship.get("last_met_game_number", 0)),
            last_met_same_team=relationship.get("last_met_same_team"),
            last_met_my_win=relationship.get("last_met_my_win"),
            last_met_my_champion_id=str(relationship.get("last_met_my_champion_id", "")),
            last_met_other_champion_id=str(relationship.get("last_met_other_champion_id", "")),
            last_game_champion_id=str(
                local_recent.get("last_game_champion_id")
                or last_game.get("champion_id", "")
            ),
            last_game_position=str(
                local_recent.get("last_game_position")
                or last_game.get("position", "UNKNOWN")
            ),
            last_game_kills=int(
                local_recent.get("last_game_kills", last_game.get("kills", 0)) or 0
            ),
            last_game_deaths=int(
                local_recent.get("last_game_deaths", last_game.get("deaths", 0)) or 0
            ),
            last_game_assists=int(
                local_recent.get("last_game_assists", last_game.get("assists", 0)) or 0
            ),
            last_game_won=(
                local_recent.get("last_game_won")
                if has_local_recent else last_game.get("won")
            ),
            sample_scope=(
                f"OP.GG 시즌 {champion_games}경기"
                if champion_data_source == "OPGG"
                else
                f"Riot 최근 {sample_games}/{sample_target}경기"
                if champion_data_source == "RIOT_LIVE"
                else f"내 로컬 {sample_games}경기"
                if champion_data_source == "LOCAL"
                else "Riot 실시간 표본 대기"
            ),
            updated_at=updated_at,
            status="OK" if "solo_entry" in payload else "LOCAL_ONLY",
        )

    def _apply_live_profile(
        self, riot_id: str, profile: PlayerProfileStat, signature: str
    ) -> None:
        if signature != self._live_signature:
            return
        opgg_profile = self.opgg_player_profiles.get(riot_id)
        player = next(
            (item for item in self.live_game.players if item.riot_id == riot_id),
            None,
        )
        if opgg_profile and player:
            profile = self._profile_with_opgg(profile, opgg_profile, player)
        previous = self.player_profiles.get(riot_id)
        self.player_profiles[riot_id] = profile
        if (
            self._player_profile_render_value(previous)
            == self._player_profile_render_value(profile)
        ):
            return
        if (
            player
            and self._comparable_live_position(player.position) == "JUNGLE"
        ):
            self.root.after(10, self._ensure_jungle_tendencies)
        self.root.after(15, self._ensure_lane_opponent_analysis)
        self.root.after(20, self._ensure_my_account_analysis)
        self._schedule_play_render()

    def _apply_live_player_behavior(
        self, riot_id: str, behavior: PlayerBehaviorStat, signature: str,
    ) -> None:
        """Publish one player's cached behavior without rebuilding the board."""
        if signature != self._live_signature:
            return
        previous = getattr(self, "player_behaviors", {}).get(riot_id)
        self.player_behaviors[riot_id] = behavior
        if previous == behavior:
            return
        self._schedule_play_render()

    @staticmethod
    def _live_team_groups(
        players: list[LivePlayer], active_team: str,
    ) -> tuple[list[LivePlayer], list[LivePlayer]]:
        return (
            [player for player in players if player.team == active_team],
            [player for player in players if player.team != active_team],
        )

    @staticmethod
    def _draft_pick_side_payload(members: list[DraftMember]) -> dict[str, list[int]]:
        return {
            member.champion_id: [int(member.pick_order), int(member.pick_turn)]
            for member in members
            if member.champion_id and member.pick_order is not None and member.pick_turn is not None
        }

    def _remember_draft_pick_context(self, draft: DraftSnapshot) -> None:
        ally = self._draft_pick_side_payload(draft.ally_team_order)
        enemy = self._draft_pick_side_payload(draft.enemy_team_order)
        if len(ally) != 5 or len(enemy) != 5:
            return
        context = {"ally": ally, "enemy": enemy}
        self._cached_draft_pick_context = context
        self._draft_pick_context_cache_loaded = True
        signature = json.dumps(context, ensure_ascii=False, sort_keys=True)
        if signature == self._draft_pick_context_signature:
            return
        self._draft_pick_context_signature = signature
        payload = {
            **context,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.storage.set_setting(
            "last_draft_pick_context",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def _draft_pick_context_candidates(self) -> list[dict[str, dict[str, list[int]]]]:
        candidates: list[dict[str, dict[str, list[int]]]] = []
        current = {
            "ally": self._draft_pick_side_payload(self.draft.ally_team_order),
            "enemy": self._draft_pick_side_payload(self.draft.enemy_team_order),
        }
        if current["ally"] and current["enemy"]:
            candidates.append(current)
        if not getattr(self, "_draft_pick_context_cache_loaded", False):
            raw = self.storage.get_setting("last_draft_pick_context")
            try:
                cached = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                cached = {}
            if isinstance(cached, dict):
                self._cached_draft_pick_context = {
                    "ally": dict(cached.get("ally") or {}),
                    "enemy": dict(cached.get("enemy") or {}),
                }
            else:
                self._cached_draft_pick_context = None
            self._draft_pick_context_cache_loaded = True
        cached_context = getattr(self, "_cached_draft_pick_context", None)
        if cached_context and cached_context not in candidates:
            candidates.append(cached_context)
        return candidates

    def _attach_draft_pick_context(self, snapshot: LiveGameSnapshot) -> bool:
        allies, enemies = self._live_team_groups(snapshot.players, snapshot.active_team)
        ally_ids = {player.champion_id for player in allies if player.champion_id}
        enemy_ids = {player.champion_id for player in enemies if player.champion_id}
        if len(ally_ids) != 5 or len(enemy_ids) != 5:
            return False
        for context in self._draft_pick_context_candidates():
            ally_context = context.get("ally") or {}
            enemy_context = context.get("enemy") or {}
            if set(ally_context) != ally_ids or set(enemy_context) != enemy_ids:
                continue
            for player in allies:
                values = ally_context.get(player.champion_id) or []
                if len(values) >= 2:
                    player.draft_team_pick_order = int(values[0])
                    player.draft_pick_turn = int(values[1])
            for player in enemies:
                values = enemy_context.get(player.champion_id) or []
                if len(values) >= 2:
                    player.draft_team_pick_order = int(values[0])
                    player.draft_pick_turn = int(values[1])
            return True
        return False

    def _check_live_duos(self) -> None:
        """Refresh current-player samples and check same-team duo evidence.

        Fetching one Match-v5 ID page returns 100 IDs. Match detail calls are
        capped below the development-key window: at most five recent matches per
        other player first, with the remaining budget reserved for duo evidence.
        """
        if (
            self.demo
            or self._duo_checking
            or not self.live_game.players
            or self._duo_checked_signature == self._live_signature
        ):
            return
        signature = self._live_signature
        players = sorted(
            (
                player for player in self.live_game.players
                if live_identity_available(player)
            ),
            key=lambda player: (
                not player.is_active_player,
                player.team != self.live_game.active_team,
                player.position,
            ),
        )
        if not players:
            self._duo_checked_signature = self._live_signature
            self.live_duo_status.configure(
                text="DUO 확인 불가 · 로딩 구간 Riot ID가 비공개였습니다.",
                fg=COLORS["muted"],
            )
            return
        api_key = self.storage.get_setting("riot_api_key")
        if not api_key or self.storage.riot_api_key_needs_refresh():
            self.live_duo_status.configure(
                text="DUO 추정 확인 불가 · Riot 개발용 API 키를 갱신하세요.",
                fg=COLORS["red"],
            )
            return
        active_team = self.live_game.active_team
        team_groups = self._live_team_groups(players, active_team)
        self._duo_checking = True
        self.live_duo_status.configure(
            text="Riot 현 챔프 표본·DUO 확인 중 0/10명", fg=COLORS["blue"]
        )

        def work() -> tuple[dict[str, list[tuple[str, str, str]]], int, int, int, int]:
            client = RiotApiClient(api_key)
            analysis_max_age = self._request_max_age(
                "player_analysis_cooldown_hours"
            )
            puuids: dict[str, str] = {}
            histories: dict[str, list[str]] = {}
            opgg_histories: dict[str, list[str]] = {}
            rank_updates = 0
            fetched_details = 0
            champion_updates = 0
            my_puuid = self.storage.get_setting("riot_puuid")
            for index, player in enumerate(players, start=1):
                if not player.riot_game_name or not player.riot_tag_line:
                    continue
                opgg_profile = self.storage.load_opgg_player_profile(
                    player.riot_id, max_age=analysis_max_age,
                )
                if opgg_profile:
                    opgg_histories[player.riot_id] = [
                        item.match_id
                        for item in completed_solo_ranked_matches(
                            opgg_profile.recent_matches,
                        )
                        if item.match_id
                    ]
                puuid = self.storage.find_puuid_by_riot_id(player.riot_id)
                if not riot_puuid_is_canonical(puuid):
                    try:
                        account = client.resolve_account(
                            player.riot_game_name, player.riot_tag_line,
                        )
                    except RiotApiError as exc:
                        if riot_authentication_error(exc):
                            raise
                        histories[player.riot_id] = []
                        self._post_ui(
                            lambda riot_id=player.riot_id:
                            self._apply_live_profile(
                                riot_id, unavailable_player_profile(), signature,
                            )
                        )
                        self._post_ui(
                            lambda done=index, total=len(players):
                            self.live_duo_status.configure(
                                text=f"Riot 현 챔프 표본·DUO 확인 중 {done}/{total}명",
                                fg=COLORS["blue"],
                            )
                        )
                        continue
                    puuid = str(account.get("puuid") or "")
                    if puuid:
                        self.storage.save_player_identity(player.riot_id, puuid)
                if puuid:
                    puuids[player.riot_id] = puuid
                    if player.is_active_player:
                        my_puuid = puuid
                    recent_profile = self.storage.load_live_profile(
                        player.riot_id, max_age=analysis_max_age
                    )
                    existing = recent_profile or self.storage.load_live_profile_any_age(
                        player.riot_id
                    )
                    payload = dict(existing[1]) if existing else {}
                    if not recent_profile:
                        try:
                            entries = client.league_entries_by_puuid(
                                puuid, platform="kr",
                            )
                        except RiotApiError as exc:
                            if riot_authentication_error(exc):
                                raise
                            entries = []
                            payload["rank_unavailable"] = True
                        solo_entry = next(
                            (
                                entry for entry in entries
                                if entry.get("queueType") == "RANKED_SOLO_5x5"
                            ),
                            {},
                        )
                        payload["solo_entry"] = solo_entry
                        payload["rank_checked"] = True
                        self.storage.save_live_profile(player.riot_id, puuid, payload)
                        rank_updates += 1
                        partial = self._make_cached_player_profile(
                            puuid,
                            payload,
                            datetime.now().isoformat(timespec="seconds"),
                            player.champion_id,
                            player.is_active_player,
                            analysis_max_age,
                        )
                        self._post_ui(
                            lambda riot_id=player.riot_id, profile=partial:
                            self._apply_live_profile(riot_id, profile, signature)
                        )
                    history = recent_match_ids_from_payload(
                        payload, max_age=analysis_max_age
                    )
                    if history is None:
                        stale_history = recent_match_ids_from_payload(
                            payload, max_age=None
                        ) or []
                        try:
                            history = client.match_ids(puuid, count=100)
                            payload["recent_match_ids"] = history
                            payload["recent_match_ids_checked_at"] = (
                                datetime.now().isoformat(timespec="seconds")
                            )
                            self.storage.save_live_profile(
                                player.riot_id, puuid, payload
                            )
                        except RiotApiError as exc:
                            if riot_authentication_error(exc):
                                raise
                            history = stale_history
                    histories[player.riot_id] = history
                    remote_sample = (
                        None if player.is_active_player
                        else live_champion_sample_from_payload(
                            payload, player.champion_id,
                            max_age=analysis_max_age,
                        )
                    )
                    if not player.is_active_player and not remote_sample:
                        sample_ids = history[:LIVE_CHAMPION_SAMPLE_MATCHES]
                        for match_id in sample_ids:
                            if self.storage.load_match(match_id) is not None:
                                continue
                            if fetched_details >= LIVE_PROFILE_DETAIL_BUDGET:
                                break
                            try:
                                detail = client.match(match_id)
                            except RiotApiError as exc:
                                if riot_authentication_error(exc):
                                    raise
                                continue
                            self.storage.save_matches([detail])
                            fetched_details += 1
                        inspected, champion_games, champion_wins = (
                            self.storage.player_champion_record_for_matches(
                                puuid, player.champion_id, sample_ids
                            )
                        )
                        if inspected:
                            payload.update({
                                "live_champion_id": player.champion_id,
                                "live_champion_sample_games": inspected,
                                "live_champion_sample_target": len(sample_ids),
                                "live_champion_games": champion_games,
                                "live_champion_wins": champion_wins,
                                "live_champion_checked_at": datetime.now().isoformat(
                                    timespec="seconds"
                                ),
                            })
                            self.storage.save_live_profile(
                                player.riot_id, puuid, payload
                            )
                            champion_updates += 1
                    profile = self._make_player_profile(
                        player,
                        puuid,
                        payload,
                        my_puuid,
                        datetime.now().isoformat(timespec="seconds"),
                    )
                    self._post_ui(
                        lambda riot_id=player.riot_id, completed=profile:
                        self._apply_live_profile(riot_id, completed, signature)
                    )
                self._post_ui(
                    lambda done=index, total=len(players): self.live_duo_status.configure(
                        text=f"Riot 현 챔프 표본·DUO 확인 중 {done}/{total}명",
                        fg=COLORS["blue"],
                    )
                )

            pairs: dict[str, list[tuple[str, str, str]]] = {}
            for team_players in team_groups:
                for pair_index, first in enumerate(team_players):
                    first_puuid = puuids.get(first.riot_id, "")
                    first_history = histories.get(first.riot_id, [])
                    for second in team_players[pair_index + 1:]:
                        second_puuid = puuids.get(second.riot_id, "")
                        second_history = histories.get(second.riot_id, [])
                        same_team_positions: list[tuple[int, int]] = []
                        if (
                            first_puuid and second_puuid
                            and first_history and second_history
                        ):
                            second_ids = set(second_history)
                            common_ids = [
                                match_id for match_id in first_history
                                if match_id in second_ids
                            ][:5]
                            first_positions = {
                                match_id: position
                                for position, match_id in enumerate(first_history)
                            }
                            second_positions = {
                                match_id: position
                                for position, match_id in enumerate(second_history)
                            }
                            for match_id in common_ids:
                                match = self.storage.load_match(match_id)
                                if match is None:
                                    if fetched_details >= LIVE_TOTAL_DETAIL_BUDGET:
                                        break
                                    try:
                                        match = client.match(match_id)
                                    except RiotApiError as exc:
                                        if riot_authentication_error(exc):
                                            raise
                                        continue
                                    self.storage.save_matches([match])
                                    fetched_details += 1
                                participants = match.get("info", {}).get("participants", [])
                                first_row = next(
                                    (row for row in participants if row.get("puuid") == first_puuid), None
                                )
                                second_row = next(
                                    (row for row in participants if row.get("puuid") == second_puuid), None
                                )
                                if (
                                    first_row
                                    and second_row
                                    and first_row.get("teamId") == second_row.get("teamId")
                                ):
                                    same_team_positions.append(
                                        (first_positions[match_id], second_positions[match_id])
                                    )
                                    if {(0, 0), (1, 1)}.issubset(set(same_team_positions)):
                                        break
                        classification = self._classify_duo_evidence(same_team_positions)
                        if not classification:
                            first_opgg = opgg_histories.get(first.riot_id, [])
                            second_opgg = opgg_histories.get(second.riot_id, [])
                            second_opgg_positions = {
                                match_id: position
                                for position, match_id in enumerate(second_opgg)
                            }
                            overlap_positions = [
                                (position, second_opgg_positions[match_id])
                                for position, match_id in enumerate(first_opgg)
                                if match_id in second_opgg_positions
                            ][:5]
                            classification = self._classify_duo_overlap_evidence(
                                overlap_positions,
                            )
                        if classification:
                            level, evidence = classification
                            pairs.setdefault(first.riot_id, []).append(
                                (second.riot_id, level, evidence)
                            )
                            pairs.setdefault(second.riot_id, []).append(
                                (first.riot_id, level, evidence)
                            )
            return pairs, len(puuids), fetched_details, rank_updates, champion_updates

        def success(
            result: tuple[dict[str, list[tuple[str, str, str]]], int, int, int, int]
        ) -> None:
            self._duo_checking = False
            pairs, resolved, details, rank_updates, champion_updates = result
            if signature != self._live_signature:
                self.root.after(100, self._check_live_duos)
                return
            self._duo_checked_signature = signature
            self.duo_pairs = pairs
            if pairs:
                duo_count = len({
                    visual[0]
                    for visual in duo_group_visuals(
                        self.live_game.players, pairs,
                        active_team=self.live_game.active_team,
                    ).values()
                })
                text = (
                    f"DUO 추정 완료 · 같은 색끼리 {duo_count}쌍 · 현재 {resolved}명 확인 · "
                    f"현 챔프 {champion_updates}명 갱신 · 상세 {details}건 요청"
                )
            else:
                text = (
                    f"DUO 추정 없음 · 현재 {resolved}명 확인 · "
                    f"현 챔프 {champion_updates}명 갱신 · 상세 {details}건 요청"
                )
            self.live_duo_status.configure(text=text, fg=COLORS["orange"])
            # Newly fetched Riot match details can improve both behavior
            # analyses. Keep the previous cards visible while one background
            # recomputation replaces only the insight section.
            self._jungle_tendency_context = None
            self._lane_opponent_analysis_context = None
            self._my_account_analysis_context = None
            self._load_live_profiles()
            self.root.after(80, self._ensure_jungle_tendencies)
            self.root.after(90, self._ensure_lane_opponent_analysis)
            self.root.after(100, self._ensure_my_account_analysis)
            self._render_play()

        def error(exc: Exception) -> None:
            self._duo_checking = False
            key_expired = isinstance(exc, RiotApiError) and "만료" in str(exc)
            if key_expired:
                self.storage.mark_riot_api_key_invalid()
                self._render_header()
            self.live_duo_status.configure(text=f"DUO 추정 중단 · {exc}", fg=COLORS["red"])
            if not key_expired and signature == self._live_signature:
                self.root.after(15000, self._check_live_duos)

        self._background(work, success, error)

    @staticmethod
    def _classify_duo_evidence(
        same_team_positions: list[tuple[int, int]],
    ) -> tuple[str, str] | None:
        positions = set(same_team_positions)
        if {(0, 0), (1, 1)}.issubset(positions):
            return "매우 유력", "서로의 직전 2경기가 모두 동팀"
        if (0, 0) in positions:
            return "유력", "직전판도 동팀 · 현재 포함 2연속"
        if any(max(first_index, second_index) <= 1 for first_index, second_index in positions):
            return "유력", "양쪽 최근 2경기 안에 동팀 · 현재도 같은 팀"
        ordered = sorted(positions)
        if any(
            abs(first[0] - second[0]) == 1 and abs(first[1] - second[1]) == 1
            for index, first in enumerate(ordered)
            for second in ordered[index + 1:]
        ):
            return "유력", "최근 기록에서 2경기 연속 동팀"
        if len(positions) >= 2:
            return "가능", f"최근 100경기 중 동팀 {len(positions)}회 이상 확인"
        if any(max(first_index, second_index) <= 4 for first_index, second_index in positions):
            return "가능", "양쪽 최근 5경기 안에 동팀 · 현재도 같은 팀"
        return None

    @staticmethod
    def _classify_duo_overlap_evidence(
        shared_match_positions: list[tuple[int, int]],
    ) -> tuple[str, str] | None:
        """Use OP.GG shared match IDs when Riot details are late/unavailable.

        A single common match can be an opposing-team coincidence, so it is
        only a possible signal. Two aligned recent matches while the players
        are teammates in the current game is strong premade evidence.
        """
        positions = set(shared_match_positions)
        if {(0, 0), (1, 1)}.issubset(positions):
            return "매우 유력", "OP.GG 서로의 직전 2경기 동일 · 현재도 같은 팀"
        ordered = sorted(positions)
        if any(
            abs(first[0] - second[0]) == 1
            and abs(first[1] - second[1]) == 1
            for index, first in enumerate(ordered)
            for second in ordered[index + 1:]
        ):
            return "유력", "OP.GG 최근 기록에서 2경기 연속 함께 큐"
        if len(positions) >= 2:
            return "가능", f"OP.GG 최근 기록 {len(positions)}경기 동일"
        if (0, 0) in positions:
            return "가능", "OP.GG 직전 경기 동일 · 현재도 같은 팀"
        return None

    def _tick(self) -> None:
        self._render_header()
        self.root.after(1000, self._tick)

    def _demo_history_overview(self) -> HistoryOverview:
        """Create screenshot-safe solo-queue history using fictional players."""
        demo_puuid = "demo-player-puuid"
        now_ms = int(time.time() * 1000)
        games = [
            ("Janna", True, 2, 3, 18, 38, (3870, 3158, 6617, 3107, 2055)),
            ("Thresh", True, 1, 5, 16, 46, (3860, 3117, 3190, 3107, 2055)),
            ("Leona", False, 1, 8, 11, 41, (3860, 3009, 3190, 3109, 2055)),
            ("Nami", True, 3, 4, 19, 52, (3870, 3158, 6620, 3107, 2055)),
            ("Braum", False, 0, 6, 13, 44, (3860, 3117, 3190, 3109, 2055)),
            ("Janna", True, 1, 2, 21, 61, (3870, 3158, 6617, 3222, 2055)),
            ("Thresh", False, 2, 7, 9, 35, (3860, 3117, 3109, 3050, 2055)),
            ("Leona", True, 4, 5, 17, 48, (3860, 3009, 3190, 3107, 2055)),
            ("Nami", True, 2, 3, 20, 57, (3870, 3158, 6620, 3222, 2055)),
            ("Braum", False, 1, 9, 10, 39, (3860, 3117, 3190, 3075, 2055)),
            ("Janna", True, 0, 4, 23, 66, (3870, 3158, 6617, 3107, 2055)),
            ("Thresh", False, 3, 8, 12, 43, (3860, 3117, 3190, 3109, 2055)),
        ]
        ally_champions = ("Garen", "LeeSin", "Ahri", "Jinx")
        enemy_champions = ("Darius", "Viego", "Syndra", "Samira", "Leona")
        positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM")
        matches: list[dict[str, object]] = []

        for index, (champion, won, kills, deaths, assists, vision, items) in enumerate(games):
            duration = 1_470 + index * 37
            my_team = 100
            mine = {
                "puuid": demo_puuid,
                "teamId": my_team,
                "championName": champion,
                "teamPosition": "UTILITY",
                "riotIdGameName": "DemoPlayer",
                "riotIdTagline": "DEMO",
                "win": won,
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "totalMinionsKilled": 29 + index * 2,
                "neutralMinionsKilled": 0,
                "visionScore": vision,
                "wardsPlaced": 14 + index,
                "detectorWardsPlaced": 2 + index % 4,
                "timeCCingOthers": 18 + index * 2,
                "totalDamageDealtToChampions": 7_200 + index * 530,
                "totalDamageTaken": 11_400 + index * 410,
                "damageSelfMitigated": 9_600 + index * 570,
                "goldEarned": 8_300 + index * 260,
                "summoner1Id": 4,
                "summoner2Id": 14 if index % 3 else 3,
                "timePlayed": duration,
                "perks": {
                    "styles": [
                        {
                            "description": "primary", "style": 8400,
                            "selections": [{"perk": 8351}]
                        },
                        {
                            "description": "subStyle", "style": 8300,
                            "selections": [{"perk": 8340}]
                        },
                    ]
                },
            }
            for item_index, item_id in enumerate(items):
                mine[f"item{item_index}"] = item_id
            participants: list[dict[str, object]] = [mine]
            for slot, (ally_champion, position) in enumerate(
                zip(ally_champions, positions), start=1
            ):
                participants.append({
                    "puuid": f"demo-ally-{index}-{slot}",
                    "teamId": my_team,
                    "championName": ally_champion,
                    "teamPosition": position,
                    "riotIdGameName": f"Ally{slot}",
                    "riotIdTagline": "DEMO",
                    "win": won,
                    "kills": 3 + (slot + index) % 7,
                    "deaths": 2 + (slot * 2 + index) % 6,
                    "assists": 4 + (slot + index) % 9,
                })
            for slot, enemy_champion in enumerate(enemy_champions, start=1):
                participants.append({
                    "puuid": f"demo-enemy-{index}-{slot}",
                    "teamId": 200,
                    "championName": enemy_champion,
                    "teamPosition": (*positions, "UTILITY")[slot - 1],
                    "riotIdGameName": f"Enemy{slot}",
                    "riotIdTagline": "DEMO",
                    "win": not won,
                    "kills": 2 + (slot + index) % 6,
                    "deaths": 3 + (slot * 2 + index) % 7,
                    "assists": 3 + (slot + index) % 8,
                })
            match_id = f"DEMO_{12_000 - index}"
            matches.append({
                "metadata": {"matchId": match_id},
                "info": {
                    "gameId": 12_000 - index,
                    "gameCreation": now_ms - index * 7_200_000,
                    "gameEndTimestamp": now_ms - index * 7_200_000 + duration * 1000,
                    "gameDuration": duration,
                    "queueId": 420,
                    "participants": participants,
                    "teams": [
                        {
                            "teamId": 100, "win": won,
                            "objectives": {
                                "dragon": {"kills": 2 + index % 3},
                                "baron": {"kills": int(index % 2 == 0)},
                                "riftHerald": {"kills": int(index % 3 == 0)},
                                "horde": {"kills": 3 + index % 4},
                                "tower": {"kills": 6 + index % 4},
                            },
                        },
                        {
                            "teamId": 200, "win": not won,
                            "objectives": {
                                "dragon": {"kills": 1 + index % 2},
                                "baron": {"kills": int(index % 2 == 1)},
                                "riftHerald": {"kills": int(index % 3 != 0)},
                                "horde": {"kills": 2 + index % 3},
                                "tower": {"kills": 3 + index % 5},
                            },
                        },
                    ],
                },
            })

        # The demo Storage belongs to a TemporaryDirectory, so these rows are
        # discarded at exit and can never mix with data/advisor.db.
        self.storage.save_matches(matches)
        overview = analyze_history(matches, demo_puuid)
        for index, entry in enumerate(overview.entries):
            entry.lp_delta = (19 + index % 4) if entry.won else -(17 + index % 5)
            entry.lp_confidence = "EXACT"
            entry.lp_before_rank = f"EMERALD II {42 + index}LP"
            entry.lp_after_rank = f"EMERALD II {42 + index + entry.lp_delta}LP"
            entry.predicted_win_rate = 52.0 + ((index * 7) % 13) if entry.won else 47.0
            entry.predicted_win = entry.predicted_win_rate >= 50.0
            entry.prediction_confidence = "데모"
            entry.prediction_correct = entry.predicted_win == entry.won
        refresh_recent_20_summary(overview)
        return overview

    def _demo_draft(self) -> DraftSnapshot:
        top = DraftMember("Ornn", "오른", "TOP", "HOVER", 0, 1, 1)
        jungle = DraftMember("LeeSin", "리 신", "JUNGLE", "LOCKED", 1, 2, 3)
        middle = DraftMember("Syndra", "신드라", "MIDDLE", "LOCKED", 2, 3, 3)
        bottom = DraftMember("Jinx", "징크스", "BOTTOM", "HOVER", 3, 4, 5)
        support = DraftMember("Janna", "잔나", "SUPPORT", "HOVER", 4, 5, 5)
        enemy_team = [
            DraftMember("Darius", "다리우스", "TOP", "LOCKED", 5, 1, 2),
            DraftMember("Viego", "비에고", "JUNGLE", "LOCKED", 6, 2, 2),
            DraftMember("Katarina", "카타리나", "MIDDLE", "LOCKED", 7, 3, 4),
            DraftMember("Samira", "사미라", "BOTTOM", "LOCKED", 8, 4, 4),
            DraftMember("Leona", "레오나", "SUPPORT", "LOCKED", 9, 5, 6),
        ]
        draft = DraftSnapshot(
            my_pick_order=5,
            my_status="SELECTING",
            ally_locked=[jungle, middle],
            ally_hover=[top, bottom],
            my_hover=support,
            enemy_locked=enemy_team,
            ally_team_order=[top, jungle, middle, bottom, support],
            enemy_team_order=enemy_team,
            ally_bans=["Zed", "Yuumi", "Aatrox", "Akali", "Nidalee"],
            enemy_bans=["Thresh", "Lulu", "Blitzcrank", "Nami", "Pyke"],
            ally_ban_actions=[
                DraftBan(champion_id, name, "LOCKED", order=order)
                for order, (champion_id, name) in enumerate(
                    [("Zed", "제드"), ("Yuumi", "유미"), ("Aatrox", "아트록스"),
                     ("Akali", "아칼리"), ("Nidalee", "니달리")], start=1
                )
            ],
            enemy_ban_actions=[
                DraftBan(champion_id, name, "LOCKED", order=order)
                for order, (champion_id, name) in enumerate(
                    [("Thresh", "쓰레쉬"), ("Lulu", "룰루"),
                     ("Blitzcrank", "블리츠크랭크"), ("Nami", "나미"),
                     ("Pyke", "파이크")], start=1
                )
            ],
            selected_enemy_support_id="Leona",
            selected_enemy_support_name_ko="레오나",
            selected_enemy_support_source="AUTO_ENEMY_SUPPORT",
            local_player_cell_id=4,
            connection_state="CHAMP_SELECT",
        )
        draft.refresh_snapshot_id()
        return draft

    def _demo_live_game(self) -> LiveGameSnapshot:
        data = [
            ("Player", "KR1", "Janna", "잔나", "ORDER", "UTILITY", True),
            ("TopPlayer", "KR2", "Ornn", "오른", "ORDER", "TOP", False),
            ("JunglePlayer", "KR3", "LeeSin", "리 신", "ORDER", "JUNGLE", False),
            ("MidPlayer", "KR4", "Syndra", "신드라", "ORDER", "MIDDLE", False),
            ("AdcPlayer", "KR5", "Jinx", "징크스", "ORDER", "BOTTOM", False),
            ("EnemyTop", "KR6", "Darius", "다리우스", "CHAOS", "TOP", False),
            ("EnemyJungle", "KR7", "Viego", "비에고", "CHAOS", "JUNGLE", False),
            ("EnemyMid", "KR8", "Katarina", "카타리나", "CHAOS", "MIDDLE", False),
            ("EnemyAdc", "KR9", "Samira", "사미라", "CHAOS", "BOTTOM", False),
            ("EnemySupport", "KR10", "Leona", "레오나", "CHAOS", "UTILITY", False),
        ]
        return LiveGameSnapshot(
            players=[
                LivePlayer(
                    champion_id=champion_id,
                    champion_name_ko=name_ko,
                    riot_game_name=name,
                    riot_tag_line=tag,
                    team=team,
                    position=position,
                    is_active_player=active,
                )
                for name, tag, champion_id, name_ko, team, position, active in data
            ],
            active_riot_id="Player#KR1",
            active_team="ORDER",
            game_time=614,
            game_mode="CLASSIC",
        )

    def _demo_player_profiles(self) -> dict[str, PlayerProfileStat]:
        result: dict[str, PlayerProfileStat] = {}
        for index, player in enumerate(self._demo_live_game().players):
            result[player.riot_id] = PlayerProfileStat(
                puuid=f"demo-{index}",
                tier="PLATINUM" if index < 5 else "EMERALD",
                rank="II" if index % 2 == 0 else "III",
                league_points=34 + index,
                season_wins=28 + index,
                season_losses=24 + (index % 4),
                champion_games=3 + (index % 8),
                champion_wins=min(2 + (index % 5), 3 + (index % 8)),
                local_sample_games=20,
                together_games=0 if index == 0 else index % 4,
                together_wins=(
                    0 if index == 0 else min(index % 3, index % 4)
                ),
                against_games=0 if index < 5 else (index - 4),
                against_my_wins=0 if index < 5 else (index - 5) // 2,
                recent_10_together_games=0 if index == 0 else index % 2,
                recent_10_against_games=0 if index < 5 else 1,
                last_met_game_number=0 if index == 0 else (1 if index in {2, 8} else index + 2),
                last_met_same_team=index < 5,
                last_met_my_win=index % 2 == 0,
                last_met_my_champion_id="Janna",
                last_met_other_champion_id=player.champion_id,
                last_game_champion_id=("Nami" if index % 2 == 0 else "Thresh"),
                last_game_position=player.position,
                last_game_kills=1 + index % 4,
                last_game_deaths=index % 5,
                last_game_assists=8 + index,
                last_game_won=index % 2 == 0,
                sample_scope="저장 20경기",
                updated_at=datetime.now().isoformat(timespec="seconds"),
                status="OK",
            )
        return result

    def _demo_duo_pairs(self) -> dict[str, list[tuple[str, str, str]]]:
        return {
            "TopPlayer#KR2": [("JunglePlayer#KR3", "가능", "최근 100경기 중 동팀 3회")],
            "JunglePlayer#KR3": [("TopPlayer#KR2", "가능", "최근 100경기 중 동팀 3회")],
            "EnemyAdc#KR9": [("EnemySupport#KR10", "매우 유력", "직전 2경기가 모두 동팀")],
            "EnemySupport#KR10": [("EnemyAdc#KR9", "매우 유력", "직전 2경기가 모두 동팀")],
        }

    def _demo_lane_matchups(self) -> dict[str, LaneMatchupStat]:
        demo_rates = {
            "TOP": (48.4, 6210),
            "JUNGLE": (51.3, 8420),
            "MIDDLE": (52.6, 4370),
            "BOTTOM": (49.2, 9150),
            "SUPPORT": (53.6, 8420),
        }
        result: dict[str, LaneMatchupStat] = {}
        for position, ally, enemy in self._live_lane_pairs(self.live_game):
            win_rate, games = demo_rates[position]
            demo_laning = {
                "TOP": 46.9, "JUNGLE": None, "MIDDLE": 54.1,
                "BOTTOM": 50.4, "SUPPORT": 55.0,
            }[position]
            result[position] = LaneMatchupStat(
                position=position,
                ally_champion_id=ally.champion_id,
                ally_champion_name_ko=ally.champion_name_ko,
                enemy_champion_id=enemy.champion_id,
                enemy_champion_name_ko=enemy.champion_name_ko,
                ally_win_rate=win_rate,
                ally_laning_win_rate=demo_laning,
                games=games,
                patch="DEMO",
                updated_at=datetime.now().isoformat(timespec="seconds"),
                status="OK",
            )
        return result

    def _demo_jungle_tendencies(self) -> dict[str, JungleTendencyStat]:
        junglers = [
            player for player in self.live_game.players
            if self._comparable_live_position(player.position) == "JUNGLE"
        ]
        result: dict[str, JungleTendencyStat] = {}
        for player in junglers:
            ally = player.team == self.live_game.active_team
            result[player.riot_id] = JungleTendencyStat(
                puuid=f"demo:{player.riot_id}",
                champion_id=player.champion_id,
                games=12 if ally else 9,
                champion_specific=True,
                early_takedowns=2.3 if ally else 1.5,
                early_lane_kills=0.9 if ally else 0.4,
                jungle_cs_10=49.0 if ally else 56.0,
                enemy_jungle_cs=12.4 if ally else 7.2,
                spawn_objectives=0.42 if ally else 0.21,
                labels=(
                    ["갱킹 자주 감", "카정 잦음", "오브젝트 즉시"]
                    if ally else ["풀캠·성장 우선"]
                ),
                status="OK",
                message="현재 챔피언 표본",
            )
        return result

    def _demo_player_behavior(self) -> PlayerBehaviorStat:
        active = next(
            (player for player in self.live_game.players if player.is_active_player),
            None,
        )
        position = self._comparable_live_position(active.position) if active else "SUPPORT"
        opponent = next(
            (
                player for player in self.live_game.enemies
                if self._comparable_live_position(player.position) == position
            ),
            None,
        )
        return PlayerBehaviorStat(
            puuid=f"demo:{opponent.riot_id if opponent else 'opponent'}",
            champion_id=opponent.champion_id if opponent else "Leona",
            position=position,
            games=8,
            champion_specific=True,
            first_blood_kills=2,
            first_blood_assists=1,
            first_blood_rate=37.5,
            early_advantage_rate=62.5,
            early_takedowns=1.8,
            kill_participation=68.0,
            average_deaths=5.1,
            vision_per_minute=1.92,
            control_wards=4.4,
            labels=[
                "퍼블을 자주 땀", "초반 교전 잦음", "초반 라인 강함",
                "합류 잦음", "시야 좋음", "제어 와드 적극",
            ],
            status="OK",
            message="현재 챔피언 최근 표본",
        )

    def _demo_my_personal_stat(self) -> PersonalStat:
        return PersonalStat(
            games=35, wins=23, losses=12, win_rate=65.7,
            kda=3.37, vision_score=62.4,
            matchup_games=7, matchup_wins=4, matchup_losses=3,
            matchup_win_rate=57.1,
            ally_adc_games=9, ally_adc_wins=6, ally_adc_losses=3,
            ally_adc_win_rate=66.7,
        )

    def _demo_my_player_behavior(self) -> PlayerBehaviorStat:
        active = next(
            (player for player in self.live_game.players if player.is_active_player),
            None,
        )
        position = self._comparable_live_position(active.position) if active else "SUPPORT"
        return PlayerBehaviorStat(
            puuid="demo:me",
            champion_id=active.champion_id if active else "Xerath",
            position=position,
            games=14,
            champion_specific=True,
            first_blood_kills=1,
            first_blood_assists=2,
            first_blood_rate=21.4,
            early_advantage_rate=42.9,
            early_takedowns=1.2,
            kill_participation=48.0,
            average_deaths=6.3,
            vision_per_minute=1.21,
            control_wards=2.1,
            labels=["후반 합류 필요", "시야 보완 필요"],
            status="OK",
            message="현재 챔피언 최근 표본",
        )

    def _demo_build(self) -> ChampionBuildGuide:
        patch = "16.16"
        rune_ids = [8465, 8463, 8473, 8242, 8345, 8347, 5005, 5001, 5001]
        rune_names = [
            "수호자", "생명의 샘", "뼈 방패", "불굴의 의지", "비스킷 배달",
            "우주적 통찰력", "공격 속도", "체력", "체력",
        ]
        rune_build = RuneBuild(
            "추천 룬 1", 8400, 8300,
            [
                BuildAsset(
                    rune_id, name,
                    f"https://opgg-static.akamaized.net/meta/images/lol/"
                    f"{patch}.1/perk/{rune_id}.png",
                )
                for rune_id, name in zip(rune_ids, rune_names)
            ],
            games=63_801, win_rate=52.08, pick_rate=51.96,
        )
        alternate_rune_ids = [8351, 8306, 8316, 8347, 8473, 8242, 5005, 5001, 5001]
        alternate_rune_names = [
            "빙결 강화", "마법공학 점멸기", "미니언 해체분석기", "우주적 통찰력",
            "뼈 방패", "불굴의 의지", "공격 속도", "체력", "체력",
        ]
        alternate_rune_build = RuneBuild(
            "추천 룬 2", 8300, 8400,
            [
                BuildAsset(
                    rune_id, name,
                    f"https://opgg-static.akamaized.net/meta/images/lol/"
                    f"{patch}.1/perk/{rune_id}.png",
                )
                for rune_id, name in zip(alternate_rune_ids, alternate_rune_names)
            ],
            games=26_745, win_rate=53.22, pick_rate=21.78,
        )
        spells = [
            BuildAsset(
                4, "점멸",
                f"https://opgg-static.akamaized.net/meta/images/lol/{patch}.1/"
                "spell/SummonerFlash.png",
            ),
            BuildAsset(
                14, "점화",
                f"https://opgg-static.akamaized.net/meta/images/lol/{patch}.1/"
                "spell/SummonerDot.png",
            ),
        ]
        exhaust = BuildAsset(
            3, "탈진",
            f"https://opgg-static.akamaized.net/meta/images/lol/{patch}.1/"
            "spell/SummonerExhaust.png",
        )
        spell_builds = [
            SummonerSpellBuild(
                "추천 스펠 1", list(spells),
                games=82_421, win_rate=52.07, pick_rate=68.03,
            ),
            SummonerSpellBuild(
                "추천 스펠 2", [exhaust, spells[0]],
                games=21_153, win_rate=51.34, pick_rate=17.44,
            ),
        ]
        def item(item_id: int, name: str) -> BuildAsset:
            return BuildAsset(
                item_id, name,
                f"https://opgg-static.akamaized.net/meta/images/lol/"
                f"{patch}.1/item/{item_id}.png",
            )
        return ChampionBuildGuide(
            champion_id="Thresh", champion_name_ko="쓰레쉬", position="SUPPORT",
            patch="DEMO", updated_at=datetime.now().isoformat(timespec="seconds"),
            source_url="https://op.gg/lol/champions/thresh/build/support",
            rune_builds=[rune_build, alternate_rune_build], summoner_spells=spells,
            summoner_spell_builds=spell_builds,
            skill_priority=["Q", "E", "W"],
            skill_sequence=list("QEWQQRQEQREEWWR"),
            item_groups=[
                BuildItemGroup("시작 아이템", [item(3865, "세계 지도"), item(2003, "체력 물약")]),
                BuildItemGroup("신발", [item(3009, "신속의 장화"), item(3158, "명석함의 아이오니아 장화")]),
                BuildItemGroup("서포터 퀘스트 완성", [
                    item(3869, "천상의 이의"), item(3876, "피의 노래"),
                ]),
                BuildItemGroup("핵심 아이템", [
                    item(3190, "강철의 솔라리 펜던트"), item(3109, "기사의 맹세"),
                    item(3050, "지크의 융합"),
                ]),
                BuildItemGroup("상황별 아이템", [
                    item(3110, "얼어붙은 심장"), item(3075, "가시 갑옷"),
                ]),
            ],
        )

    def _demo_opgg_meta(self) -> OpggSnapshot:
        entries = [
            ("Thresh", "쓰레쉬", 52.2, 13.8, 9.3),
            ("Leona", "레오나", 51.8, 8.1, 7.5),
            ("Lulu", "룰루", 50.6, 10.0, 7.1),
            ("Nami", "나미", 51.0, 8.8, 1.0),
            ("Nautilus", "노틸러스", 49.9, 10.8, 12.4),
            ("Seraphine", "세라핀", 51.4, 4.2, 1.1),
            ("Rell", "렐", 52.0, 3.7, 2.0),
        ]
        return OpggSnapshot(
            enemy_support_id=None, enemy_support_name_ko=None, position="SUPPORT",
            region="GLOBAL", tier="EMERALD_PLUS", patch="DEMO",
            updated_at=datetime.now().isoformat(timespec="seconds"),
            source_url="https://op.gg/lol/champions?position=support&tier=emerald_plus",
            counters=[
                OpggCounter(
                    champion_id, name_ko, win_rate, 0,
                    overall_win_rate=win_rate, position_rank=index,
                    pick_rate=pick_rate, ban_rate=ban_rate,
                )
                for index, (champion_id, name_ko, win_rate, pick_rate, ban_rate)
                in enumerate(entries, start=1)
            ],
            raw_status="DEMO",
        )

    def _demo_opgg(self) -> OpggSnapshot:
        return OpggSnapshot(
            enemy_support_id="Leona", enemy_support_name_ko="레오나", region="GLOBAL",
            tier="EMERALD_PLUS", patch="DEMO", updated_at=datetime.now().isoformat(timespec="seconds"),
            source_url="https://op.gg/lol/champions/leona/counters/support",
            counters=[
                OpggCounter("Taric", "타릭", 55.1, 3840),
                OpggCounter("Janna", "잔나", 53.6, 8420),
                OpggCounter("Braum", "브라움", 52.4, 4180),
                OpggCounter("Thresh", "쓰레쉬", 52.0, 9210),
                OpggCounter("Morgana", "모르가나", 51.7, 6150),
            ],
            weak_picks=[
                OpggCounter("Nautilus", "노틸러스", 46.8, 3512),
                OpggCounter("Senna", "세나", 47.3, 3199),
                OpggCounter("Sona", "소나", 47.9, 2870),
            ],
            target_overall_win_rate=50.7, target_pick_rate=8.4, target_ban_rate=6.1,
            raw_status="DEMO",
        )

    def _demo_opgg_synergy(self) -> OpggSynergySnapshot:
        values = [
            (412, "Thresh", "쓰레쉬", 3817, 2122, 56.0, 1, 1),
            (117, "Lulu", "룰루", 3531, 1872, 53.0, 2, 1),
            (111, "Nautilus", "노틸러스", 1090, 605, 56.0, 3, 1),
            (53, "Blitzcrank", "블리츠크랭크", 963, 519, 54.0, 4, 1),
            (89, "Leona", "레오나", 725, 395, 54.0, 5, 1),
            (201, "Braum", "브라움", 483, 263, 54.0, 6, 2),
        ]
        return OpggSynergySnapshot(
            ally_champion_key=222, ally_champion_id="Jinx",
            ally_champion_name_ko="징크스",
            fetched_at=datetime.now().isoformat(timespec="seconds"),
            synergies=[
                OpggSynergyStat(
                    champion_key=key, champion_id=champion_id,
                    champion_name_ko=name, games=games, wins=wins,
                    win_rate=rate, synergy_rank=rank, synergy_tier=tier,
                )
                for key, champion_id, name, games, wins, rate, rank, tier in values
            ],
            status="OK",
        )
