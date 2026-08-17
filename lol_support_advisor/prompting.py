from __future__ import annotations

import json
import re
from typing import Any

from .champions import ChampionRegistry
from .models import (
    DraftSnapshot, OpggSnapshot, OpggSynergySnapshot, PersonalStat,
    Recommendation,
)


ROLE_NAMES = {
    "TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드",
    "BOTTOM": "원딜", "SUPPORT": "서포터", "UTILITY": "서포터",
}

MEMORY_PROMPT_VERSION = "4"

REQUEST_RULES = """LOCKED는 확정, HOVER는 픽 의사, EMPTY/UNKNOWN은 미확정이다.
밴·확정 픽·다른 아군 HOVER는 추천하지 않는다. 사용자는 모든 챔피언을 보유한다.
MANUAL은 사용자 확정, AUTO는 프로그램 추정이므로 확정처럼 단정하지 않는다.
상대 동일 포지션이 UNKNOWN이면 블라인드 안정성을 우선한다.
내 포지션이 SUPPORT이고 ally_adc_synergy가 있으면 원딜 조합 승률·표본을 반드시 비교한다.
추천 순위는 맞상대 상성 하나만 보지 말고 아군 원딜 궁합, 양 팀 전체 조합의 이니시·보호·딜 균형,
AD/AP 비중, 앞라인 유무, 픽 순서와 블라인드 안정성까지 종합한다.
HOVER 조합은 바뀔 수 있으므로 확정 픽보다 낮은 확신으로 다룬다.
입력에 없는 통계 숫자는 만들지 않고, 현재 포지션에서 쓸 수 있는 서로 다른 챔피언 3개만 추천한다."""


def build_memory_prompt() -> str:
    """Build the rules message that the user sends to a ChatGPT chat once."""
    response_example = {
        "schema_version": 2,
        "snapshot_id": "질문의 snapshot_id를 그대로 복사",
        "draft_mode": "MATCHUP/MATCHUP_TENTATIVE/BLIND",
        "recommendations": [
            {
                "rank": rank,
                "champion_id": "DataDragon 영문 ID",
                "champion_name_ko": "한국어 이름",
                "style": "플레이 유형",
                "blind_safety": "높음/보통/낮음",
                "reason": "추천 이유 한 문장",
                "team_synergy": "아군 조합 관계 한 문장",
                "lane_plan": "초반 운영 한 문장",
                "watch_for": "주의할 상대 픽 한 문장",
            }
            for rank in range(1, 4)
        ],
    }
    return (
        "LOL_PICK_MEMORY_V4\n"
        "앞으로 이 채팅에서 LOL_PICK_QUERY_V4가 오면 리그 오브 레전드 픽 추천기로 동작해.\n"
        + REQUEST_RULES
        + "\nrole은 추천할 내 포지션이고 opponent는 같은 포지션의 상대 챔피언이다. "
          "ally/enemy 항목 형식은 [챔피언ID, 포지션, 상태, 팀내픽순서, 전체픽턴]이다. "
          "ally_adc_synergy는 [서포터ID,조합승률,표본,시너지순위,티어]이고 "
          "my_local_combos는 [서포터ID,내조합승률,내표본]이다. 두 출처를 섞어 하나의 "
          "가짜 승률을 만들지 말고 각각 구분해 판단해. "
          "OP.GG 배열의 숫자는 질문에 있는 값만 인용해.\n"
          "답변은 인사말·설명·마크다운 없이 반드시 아래 패턴의 JSON만 출력해.\n"
          "LOL_SUPPORT_V2\n"
        + json.dumps(response_example, ensure_ascii=False, indent=2)
        + "\nEND_LOL_SUPPORT_V2\nEND_LOL_PICK_MEMORY_V4"
    )


def _opgg_payload(
    snapshot: OpggSnapshot | None,
    draft: DraftSnapshot,
    meta_snapshot: OpggSnapshot | None = None,
) -> dict[str, Any]:
    if not snapshot and not meta_snapshot:
        return {
            "status": "NO_CACHE",
            "enemy_support_known": bool(draft.selected_enemy_support_id),
            "notice": "OP.GG 캐시가 없으므로 숫자를 추측하지 말 것",
        }
    snapshot = snapshot or meta_snapshot
    assert snapshot is not None
    unavailable = set(draft.unavailable_champions())
    counters = []
    for entry in snapshot.counters:
        item = entry.to_dict()
        item["availability"] = "UNAVAILABLE" if entry.champion_id in unavailable else "AVAILABLE"
        counters.append(item)
    weak = []
    for entry in snapshot.weak_picks:
        item = entry.to_dict()
        item["availability"] = "UNAVAILABLE" if entry.champion_id in unavailable else "AVAILABLE"
        weak.append(item)
    result = {
        "status": snapshot.raw_status,
        "region": snapshot.region,
        "tier": snapshot.tier,
        "patch": snapshot.patch,
        "updated_at": snapshot.updated_at,
        "source_url": snapshot.source_url,
        "enemy_support_known": bool(snapshot.enemy_support_id),
        "enemy_support_id": snapshot.enemy_support_id,
        "position": snapshot.position,
        "lane_opponent_known": bool(snapshot.enemy_support_id),
        "lane_opponent_id": snapshot.enemy_support_id,
        "weak_picks": weak,
        "enemy_support_overall": {
            "win_rate": snapshot.target_overall_win_rate,
            "pick_rate": snapshot.target_pick_rate,
            "ban_rate": snapshot.target_ban_rate,
        },
    }
    if snapshot.enemy_support_id:
        result["counter_picks"] = counters
    else:
        result["blind_pick_candidates"] = counters
    if meta_snapshot:
        meta_rankings = []
        for index, entry in enumerate(meta_snapshot.counters[:15], start=1):
            meta_rankings.append({
                "rank": entry.position_rank or index,
                "champion_id": entry.champion_id,
                "champion_name_ko": entry.champion_name_ko,
                "win_rate": entry.overall_win_rate,
                "pick_rate": entry.pick_rate,
                "ban_rate": entry.ban_rate,
                "availability": (
                    "UNAVAILABLE" if entry.champion_id in unavailable else "AVAILABLE"
                ),
            })
        result["position_meta"] = {
            "position": meta_snapshot.position,
            "patch": meta_snapshot.patch,
            "tier": meta_snapshot.tier,
            "updated_at": meta_snapshot.updated_at,
            "rankings": meta_rankings,
        }
    return result


def _compact_opgg_payload(
    snapshot: OpggSnapshot | None,
    draft: DraftSnapshot,
    meta_snapshot: OpggSnapshot | None = None,
    meta_limit: int = 10,
) -> dict[str, Any]:
    """Keep only the numbers useful for the next pick decision."""
    unavailable = set(draft.unavailable_champions())
    result: dict[str, Any] = {
        "status": "NO_CACHE",
        "notice": "OP.GG 캐시가 없으므로 숫자를 추측하지 말 것",
    }
    if snapshot:
        result = {
            "status": snapshot.raw_status,
            "patch": snapshot.patch,
            "matchup": [
                [
                    entry.champion_id,
                    entry.versus_win_rate,
                    entry.games,
                    "X" if entry.champion_id in unavailable else "O",
                ]
                for entry in snapshot.counters[:8]
            ],
            "weak": [
                [entry.champion_id, entry.versus_win_rate, entry.games]
                for entry in snapshot.weak_picks[:4]
            ],
        }
    if meta_snapshot:
        result["position_meta"] = [
            [
                entry.position_rank or index,
                entry.champion_id,
                entry.overall_win_rate,
                entry.pick_rate,
                entry.ban_rate,
                "X" if entry.champion_id in unavailable else "O",
            ]
            for index, entry in enumerate(
                meta_snapshot.counters[:max(1, int(meta_limit))], start=1
            )
        ]
        result["meta_patch"] = meta_snapshot.patch
    return result


def _compact_synergy_payload(
    snapshot: OpggSynergySnapshot | None,
    draft: DraftSnapshot,
    local_stats: dict[str, PersonalStat | None] | None = None,
) -> dict[str, Any]:
    ally_members = draft.ally_team_order or [
        *draft.ally_locked, *draft.ally_hover,
        *([draft.my_hover] if draft.my_hover else []),
    ]
    adc = next(
        (
            member for member in ally_members
            if member.role == "BOTTOM" and member.champion_id
            and member.state in {"LOCKED", "HOVER"}
        ),
        None,
    )
    if draft.my_role != "SUPPORT":
        return {"status": "NOT_APPLICABLE"}
    if not adc:
        return {
            "status": "ALLY_ADC_UNKNOWN",
            "instruction": "아군 원딜 미확정이므로 조합 승률 없이 전체 조합을 판단할 것",
        }
    result: dict[str, Any] = {
        "status": "NO_CACHE",
        "ally_adc": adc.champion_id,
        "ally_adc_state": adc.state,
        "notice": "조합 통계 캐시가 없으므로 숫자를 추측하지 말 것",
    }
    if not snapshot or snapshot.ally_champion_id != adc.champion_id:
        return result
    result.update({
        "status": snapshot.status,
        "fetched_at": snapshot.fetched_at,
        "candidates": [
            [
                item.champion_id,
                item.win_rate,
                item.games,
                item.synergy_rank,
                item.synergy_tier,
            ]
            for item in snapshot.synergies[:10]
            if item.champion_id not in set(draft.unavailable_champions())
        ],
        "my_local_combos": [
            [champion_id, stat.ally_adc_win_rate, stat.ally_adc_games]
            for champion_id, stat in (local_stats or {}).items()
            if stat is not None and stat.ally_adc_games
        ],
    })
    return result


def build_prompt(
    draft: DraftSnapshot,
    opgg: OpggSnapshot | None,
    meta_snapshot: OpggSnapshot | None = None,
    synergy_snapshot: OpggSynergySnapshot | None = None,
    local_synergy_stats: dict[str, PersonalStat | None] | None = None,
    meta_limit: int = 10,
) -> str:
    """Build the short per-draft query used after the memory prompt is registered."""
    draft.refresh_snapshot_id()
    source = draft.selected_enemy_support_source
    role_ko = ROLE_NAMES.get(draft.my_role, "서포터")
    unknown_opponent_instruction = (
        "적 서포터를 모르므로 임의 확정하지 말고 블라인드 안정성을 우선할 것"
        if role_ko == "서포터" else
        f"적 {role_ko} 챔피언을 모르므로 임의 확정하지 말고 블라인드 안정성을 우선할 것"
    )
    certainty = (
        "CONFIRMED" if source == "MANUAL_ENEMY_SUPPORT" else
        "TENTATIVE" if source == "AUTO_ENEMY_SUPPORT" else "UNKNOWN"
    )
    opponent_instruction = (
        "사용자가 직접 확정함" if source == "MANUAL_ENEMY_SUPPORT" else
        "공개된 적 픽을 기반으로 프로그램이 추정했으며 확정 정보가 아님"
        if source == "AUTO_ENEMY_SUPPORT" else
        unknown_opponent_instruction
    )
    draft_mode = (
        "MATCHUP" if source == "MANUAL_ENEMY_SUPPORT" and draft.selected_enemy_support_id else
        "MATCHUP_TENTATIVE" if source == "AUTO_ENEMY_SUPPORT" and draft.selected_enemy_support_id else
        "BLIND"
    )
    ally_members = draft.ally_team_order or [
        *draft.ally_locked, *draft.ally_hover,
        *([draft.my_hover] if draft.my_hover else []),
    ]
    enemy_members = draft.enemy_team_order or draft.enemy_locked

    def compact_member(member: Any) -> list[Any]:
        return [
            member.champion_id or None,
            member.role,
            member.state,
            member.pick_order,
            member.pick_turn,
        ]

    payload = {
        "snapshot_id": draft.snapshot_id,
        "draft_mode": draft_mode,
        "role": draft.my_role,
        "role_ko": role_ko,
        "my_pick_order": draft.my_pick_order,
        "my_status": draft.my_status,
        "ally": [compact_member(member) for member in ally_members],
        "enemy": [compact_member(member) for member in enemy_members],
        "bans": {"ally": draft.ally_bans, "enemy": draft.enemy_bans},
        "opponent": {
            "champion": draft.selected_enemy_support_id,
            "name_ko": draft.selected_enemy_support_name_ko,
            "source": source,
            "certainty": certainty,
            "instruction": opponent_instruction,
        },
        # Compatibility keys also make certainty immediately visible to the model.
        "enemy_support_certainty": certainty,
        "selected_lane_opponent": draft.payload()["selected_lane_opponent"],
        "opgg": _compact_opgg_payload(
            opgg, draft, meta_snapshot, meta_limit=meta_limit,
        ),
        "ally_adc_synergy": _compact_synergy_payload(
            synergy_snapshot, draft, local_synergy_stats,
        ),
        "decision_focus": [
            "lane_matchup", "ally_adc_synergy", "team_engage_and_peel",
            "damage_balance", "frontline", "pick_timing_and_blind_safety",
        ],
    }
    return (
        "LOL_PICK_QUERY_V4\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\nEND_LOL_PICK_QUERY_V4\n"
          "파일·명령·웹 도구를 사용하지 말고 위 입력만 즉시 판단해. "
          "기억한 LOL_PICK_MEMORY_V4 규칙대로 전체 조합 흐름을 종합해 정확히 3개를 "
          "LOL_SUPPORT_V2 형식으로만 답해."
    )


class ResponseError(ValueError):
    pass


class StaleResponseError(ResponseError):
    pass


def parse_response(
    text: str,
    draft: DraftSnapshot,
    registry: ChampionRegistry,
) -> list[Recommendation]:
    cleaned = text.strip().replace("```json", "").replace("```", "")
    start_marker = "LOL_SUPPORT_V2"
    end_marker = "END_LOL_SUPPORT_V2"
    start = cleaned.find(start_marker)
    end = cleaned.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise ResponseError("LOL_SUPPORT_V2 시작/종료 패턴을 찾지 못했습니다.")
    json_text = cleaned[start + len(start_marker):end].strip()
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ResponseError(f"JSON 형식이 올바르지 않습니다: {exc.msg}") from exc
    if payload.get("schema_version") != 2:
        raise ResponseError("지원하지 않는 답변 버전입니다.")
    draft.refresh_snapshot_id()
    if payload.get("snapshot_id") != draft.snapshot_id:
        raise StaleResponseError(
            f"오래된 답변입니다. 현재 {draft.snapshot_id}, 답변 {payload.get('snapshot_id', '없음')}"
        )
    items = payload.get("recommendations")
    if not isinstance(items, list) or len(items) != 3:
        raise ResponseError("추천 챔피언은 정확히 3개여야 합니다.")
    unavailable = set(draft.unavailable_champions())
    seen: set[str] = set()
    recommendations: list[Recommendation] = []
    required = {
        "rank", "champion_id", "champion_name_ko", "style", "blind_safety",
        "reason", "team_synergy", "lane_plan", "watch_for",
    }
    for item in items:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ResponseError("추천 항목에 필요한 필드가 빠져 있습니다.")
        champion_id = str(item["champion_id"])
        if champion_id in seen:
            raise ResponseError(f"중복 추천: {champion_id}")
        if champion_id in unavailable:
            raise ResponseError(f"사용할 수 없는 챔피언이 추천되었습니다: {champion_id}")
        if not registry.contains(champion_id):
            raise ResponseError(f"알 수 없는 챔피언 ID입니다: {champion_id}")
        seen.add(champion_id)
        recommendations.append(
            Recommendation(
                rank=int(item["rank"]),
                champion_id=champion_id,
                champion_name_ko=str(item["champion_name_ko"]),
                style=str(item["style"]),
                blind_safety=str(item["blind_safety"]),
                reason=str(item["reason"]),
                team_synergy=str(item["team_synergy"]),
                lane_plan=str(item["lane_plan"]),
                watch_for=str(item["watch_for"]),
            )
        )
    ranks = sorted(item.rank for item in recommendations)
    if ranks != [1, 2, 3]:
        raise ResponseError("추천 순위는 1, 2, 3이어야 합니다.")
    return sorted(recommendations, key=lambda item: item.rank)
