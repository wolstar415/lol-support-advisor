from __future__ import annotations

import json
import re
from typing import Any

from .champions import ChampionRegistry
from .models import DraftSnapshot, OpggSnapshot, Recommendation


ROLE_NAMES = {
    "TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드",
    "BOTTOM": "원딜", "SUPPORT": "서포터", "UTILITY": "서포터",
}

REQUEST_RULES_TEMPLATE = """너는 리그 오브 레전드 {role_ko} 픽 추천 분석기다.

아래 DRAFT_SNAPSHOT_V2 데이터와 OP.GG 데이터를 사용하여
현재 시점에서 선택하기 좋은 {role_ko} 챔피언을 정확히 3개 추천하라.

[판단 규칙]
1. LOCKED는 확정된 챔피언이다.
2. HOVER는 픽 의사일 뿐 확정된 챔피언이 아니다.
3. UNKNOWN 정보는 임의로 추측하지 않는다.
4. 상대 동일 포지션 챔피언 source가 MANUAL_UNKNOWN이거나 UNKNOWN이면 블라인드 픽 안정성을 우선한다.
5. source가 MANUAL_ENEMY_SUPPORT이면 사용자가 확정한 정보이므로 해당 상성을 가장 중요하게 판단한다.
6. source가 AUTO_ENEMY_SUPPORT이면 공개 픽을 보고 프로그램이 추정한 것일 뿐 확정 정보가 아니다. 해당 상성과 블라인드 안정성을 함께 보고, 추천 이유에서 추정임을 분명히 한다.
7. 아군 HOVER는 조합에 참고하되 확정된 것으로 가정하지 않는다.
8. 밴, 확정 픽, 다른 아군의 HOVER 챔피언은 절대 추천하지 않는다.
9. 사용자는 모든 챔피언을 보유하고 있으므로 보유 여부는 고려하지 않는다.
10. 숫자 통계는 입력의 OP.GG 데이터만 사용하고 새로운 숫자를 만들지 않는다.
11. 정확히 3개의 서로 다른 챔피언을 추천한다.
12. 반드시 {role_ko} 포지션에서 실제로 사용할 가치가 있는 챔피언만 추천한다.
13. 아래 JSON 이외의 문장, 인사말, 해설, 마크다운 코드 블록을 출력하지 않는다.
"""

# Kept as a public compatibility constant for callers that imported the old name.
REQUEST_RULES = REQUEST_RULES_TEMPLATE.format(role_ko="서포터")


def _opgg_payload(snapshot: OpggSnapshot | None, draft: DraftSnapshot) -> dict[str, Any]:
    if not snapshot:
        return {
            "status": "NO_CACHE",
            "enemy_support_known": bool(draft.selected_enemy_support_id),
            "notice": "OP.GG 캐시가 없으므로 숫자를 추측하지 말 것",
        }
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
    return result


def build_prompt(draft: DraftSnapshot, opgg: OpggSnapshot | None) -> str:
    draft.refresh_snapshot_id()
    payload = draft.payload()
    payload["snapshot_id"] = draft.snapshot_id
    source = draft.selected_enemy_support_source
    role_ko = ROLE_NAMES.get(draft.my_role, "서포터")
    unknown_opponent_instruction = (
        "적 서포터를 모르므로 임의 확정하지 말고 블라인드 안정성을 우선할 것"
        if role_ko == "서포터" else
        f"적 {role_ko} 챔피언을 모르므로 임의 확정하지 말고 블라인드 안정성을 우선할 것"
    )
    payload["enemy_support_certainty"] = (
        "CONFIRMED" if source == "MANUAL_ENEMY_SUPPORT" else
        "TENTATIVE" if source == "AUTO_ENEMY_SUPPORT" else "UNKNOWN"
    )
    payload["enemy_support_instruction"] = (
        "사용자가 직접 확정함" if source == "MANUAL_ENEMY_SUPPORT" else
        "공개된 적 픽을 기반으로 프로그램이 추정했으며 확정 정보가 아님"
        if source == "AUTO_ENEMY_SUPPORT" else
        unknown_opponent_instruction
    )
    payload["lane_opponent_certainty"] = payload["enemy_support_certainty"]
    payload["lane_opponent_instruction"] = payload["enemy_support_instruction"]
    payload["opgg"] = _opgg_payload(opgg, draft)
    draft_mode = (
        "MATCHUP" if source == "MANUAL_ENEMY_SUPPORT" and draft.selected_enemy_support_id else
        "MATCHUP_TENTATIVE" if source == "AUTO_ENEMY_SUPPORT" and draft.selected_enemy_support_id else
        "BLIND"
    )
    response_template = {
        "schema_version": 2,
        "snapshot_id": draft.snapshot_id,
        "draft_mode": draft_mode,
        "recommendations": [
            {
                "rank": rank,
                "champion_id": "DataDragon 영문 ID",
                "champion_name_ko": "한국어 이름",
                "style": "해당 포지션에 맞는 플레이 유형 한 가지",
                "blind_safety": "높음/보통/낮음 중 하나",
                "reason": "추천 이유 한 문장",
                "team_synergy": "현재 아군 조합과의 관계 한 문장",
                "lane_plan": "초반 운영 한 문장",
                "watch_for": "주의하거나 추가로 확인할 상대 픽 한 문장",
            }
            for rank in range(1, 4)
        ],
    }
    return (
        REQUEST_RULES_TEMPLATE.format(role_ko=role_ko)
        + "\nDRAFT_SNAPSHOT_V2\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\nEND_DRAFT_SNAPSHOT_V2\n\n다음 형식으로만 답변하라.\n\nLOL_SUPPORT_V2\n"
        + json.dumps(response_template, ensure_ascii=False, indent=2)
        + "\nEND_LOL_SUPPORT_V2"
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
