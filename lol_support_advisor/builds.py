from __future__ import annotations

from collections import Counter
import time
import uuid

from .champions import ChampionRegistry
from .lcu import LcuClient, LcuUnavailable
from .models import BuildAsset, BuildItemGroup, ChampionBuildGuide, RuneBuild


SPELL_NAME_TO_ID = {
    "cleanse": 1,
    "exhaust": 3,
    "flash": 4,
    "ghost": 6,
    "heal": 7,
    "smite": 11,
    "teleport": 12,
    "ignite": 14,
    "barrier": 21,
}


class BuildApplyError(RuntimeError):
    pass


def organized_item_set_groups(guide: ChampionBuildGuide) -> list[BuildItemGroup]:
    """Arrange the in-client shop blocks in the order used during a match."""
    early_order = ">".join(guide.skill_sequence[:4])
    if not early_order:
        early_order = ">".join(guide.skill_priority[:3]) or "데이터 없음"
    master_order = ">".join(guide.skill_priority[:3]) or "데이터 없음"

    def groups_named(*titles: str) -> list[BuildItemGroup]:
        wanted = set(titles)
        return [group for group in guide.item_groups if group.title in wanted]

    def merged_items(groups: list[BuildItemGroup]) -> list[BuildAsset]:
        result: list[BuildAsset] = []
        seen: set[int] = set()
        for group in groups:
            for item in group.items:
                item_id = int(item.asset_id)
                if item_id > 0 and item_id not in seen:
                    seen.add(item_id)
                    result.append(item)
        return result

    result: list[BuildItemGroup] = []
    starts = groups_named("시작 아이템")
    for index, group in enumerate(starts, start=1):
        suffix = f" {index}" if len(starts) > 1 else ""
        result.append(BuildItemGroup(
            f"[Advisor] 시작 아이템{suffix} · 초반 스킬 순서 {early_order}",
            list(group.items),
        ))

    result.append(BuildItemGroup(
        f"[Advisor] 소모품 · 스킬 마스터 순서 {master_order}",
        [BuildAsset(2003, "체력 물약"), BuildAsset(2055, "제어 와드")],
    ))

    core_groups = groups_named("신발", "서포터 퀘스트 완성", "핵심 아이템")
    core_items = merged_items(core_groups)
    if core_items:
        result.append(BuildItemGroup("[Advisor] 핵심 아이템 빌드", core_items))

    base_final = [
        group for group in guide.item_groups
        if group.title.startswith("기본 추천 완성 빌드")
    ]
    for index, group in enumerate(base_final, start=1):
        result.append(BuildItemGroup(
            f"[Advisor] 기본 최종 아이템 빌드 {index}", list(group.items)
        ))

    matchup_final = [
        group for group in guide.item_groups if "대응 완성 빌드" in group.title
    ]
    for index, group in enumerate(matchup_final, start=1):
        enemy = group.title.split("대응 완성 빌드", 1)[0].strip()
        result.append(BuildItemGroup(
            f"[Advisor] {enemy or '상대'} 대응 최종 빌드 {index}",
            list(group.items),
        ))

    situational_groups = groups_named(
        "완성 빌드", "4번째 아이템", "5번째 아이템", "6번째 아이템",
        "상황별 아이템",
    )
    situational_items = merged_items(situational_groups)
    if situational_items:
        result.append(BuildItemGroup("[Advisor] 상황별 아이템 후보", situational_items))

    result.append(BuildItemGroup(
        "[Advisor] 장신구",
        [
            BuildAsset(3340, "투명 와드"),
            BuildAsset(3364, "예언자형 렌즈"),
            BuildAsset(3363, "망원형 개조"),
        ],
    ))
    return [group for group in result if group.items]


class BuildApplicator:
    """Apply only user-selected pre-game settings through the local client.

    The class never picks champions, accepts queues, or performs in-game input.
    One stable LOL Advisor rune page is updated. If it does not exist, the
    client's last editable rune page is intentionally converted into it so a
    full page collection never causes another create-page failure. Item sets
    owned by the user or another app remain preserved.
    """

    def __init__(self, lcu: LcuClient, registry: ChampionRegistry) -> None:
        self.lcu = lcu
        self.registry = registry

    @staticmethod
    def rune_page_name(_guide: ChampionBuildGuide) -> str:
        # The client has a small rune-page limit. One stable page is shared by
        # every champion and updated in place whenever the user applies runes.
        return "LOL Advisor"

    @staticmethod
    def _advisor_rune_page(
        pages: object, preferred_name: str,
    ) -> dict | None:
        available = [
            page for page in (pages if isinstance(pages, list) else [])
            if isinstance(page, dict)
            and page.get("id") is not None
        ]
        candidates = [
            page for page in available
            if str(page.get("name") or "") == "LOL Advisor"
            or str(page.get("name") or "").startswith("LOL Advisor ·")
        ]
        exact = next(
            (page for page in candidates if str(page.get("name")) == preferred_name),
            None,
        )
        if exact:
            return exact
        # Reuse one page owned by this app instead of creating a page for
        # every champion and eventually hitting the League Client page cap.
        legacy = next((page for page in candidates if page.get("current")), None) or (
            candidates[0] if candidates else None
        )
        if legacy:
            return legacy
        editable = [
            page for page in available
            if page.get("isEditable") is not False
        ]
        return editable[-1] if editable else None

    def apply_runes(self, guide: ChampionBuildGuide, rune_build: RuneBuild) -> str:
        if len(rune_build.perks) != 9:
            raise BuildApplyError("적용할 룬 9개가 완전하지 않습니다.")
        name = self.rune_page_name(guide)
        payload = {
            "name": name,
            "primaryStyleId": int(rune_build.primary_style_id),
            "subStyleId": int(rune_build.sub_style_id),
            "selectedPerkIds": [int(perk.asset_id) for perk in rune_build.perks],
            "current": True,
        }
        try:
            pages = self.lcu.get("/lol-perks/v1/pages")
            existing = self._advisor_rune_page(pages, name)
            if existing and existing.get("id") is not None:
                self.lcu.put(f"/lol-perks/v1/pages/{int(existing['id'])}", payload)
                existing_name = str(existing.get("name") or "")
                return (
                    "Advisor 룬 페이지 갱신 완료"
                    if existing_name == name else
                    "기존 Advisor 룬 페이지 재사용 및 적용 완료"
                    if existing_name.startswith("LOL Advisor ·") else
                    "마지막 룬 페이지를 Advisor 페이지로 전환 및 적용 완료"
                )
            raise BuildApplyError(
                "재사용할 편집 가능한 룬 페이지를 찾지 못했습니다. 롤 클라이언트에서 "
                "편집 가능한 룬 페이지가 하나 이상 있는지 확인하세요."
            )
        except LcuUnavailable as exc:
            raise BuildApplyError(str(exc)) from exc

    @staticmethod
    def ordered_spell_ids(
        guide: ChampionBuildGuide, flash_slot: str = "F"
    ) -> list[int]:
        spell_ids = [int(spell.asset_id) for spell in guide.summoner_spells[:2]]
        preferred_index = 1 if str(flash_slot).upper() == "F" else 0
        if 4 in spell_ids and spell_ids.index(4) != preferred_index:
            spell_ids.reverse()
        return spell_ids

    def apply_spells(
        self, guide: ChampionBuildGuide, flash_slot: str = "F"
    ) -> str:
        spell_ids = self.ordered_spell_ids(guide, flash_slot)
        if len(spell_ids) != 2 or any(spell_id <= 0 for spell_id in spell_ids):
            raise BuildApplyError("적용할 소환사 주문 2개가 완전하지 않습니다.")
        try:
            self.lcu.patch(
                "/lol-champ-select/v1/session/my-selection",
                {"spell1Id": spell_ids[0], "spell2Id": spell_ids[1]},
            )
            return "소환사 주문 적용 완료"
        except LcuUnavailable as exc:
            raise BuildApplyError(
                "소환사 주문은 챔피언 선택 화면에서만 적용할 수 있습니다. " + str(exc)
            ) from exc

    @staticmethod
    def _item_blocks(groups: list[BuildItemGroup]) -> list[dict]:
        blocks: list[dict] = []
        for group in groups:
            counts = Counter(int(item.asset_id) for item in group.items if item.asset_id)
            if not counts:
                continue
            blocks.append({
                "type": group.title,
                "items": [
                    {"id": str(item_id), "count": count}
                    for item_id, count in counts.items()
                ],
                "hideIfSummonerSpell": "",
                "showIfSummonerSpell": "",
            })
        return blocks

    def apply_item_set(self, guide: ChampionBuildGuide) -> str:
        champion = self.registry.by_id.get(guide.champion_id)
        champion_key = int(champion[0]) if champion else 0
        blocks = self._item_blocks(organized_item_set_groups(guide))
        if not champion_key or not blocks:
            raise BuildApplyError("적용할 챔피언 또는 아이템 빌드 데이터가 없습니다.")
        try:
            summoner = self.lcu.get("/lol-summoner/v1/current-summoner")
            summoner_id = int((summoner or {}).get("summonerId") or 0)
            if not summoner_id:
                raise BuildApplyError("롤 클라이언트 소환사 정보를 읽지 못했습니다.")
            path = f"/lol-item-sets/v1/item-sets/{summoner_id}/sets"
            collection = self.lcu.get(path)
            if not isinstance(collection, dict):
                raise BuildApplyError("롤 아이템 세트 목록을 읽지 못했습니다.")
            stable_uid = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"lol-pick-advisor:{guide.position}:{guide.champion_id}",
            ))
            title = f"LOL Advisor · {guide.champion_name_ko} · {guide.position}"
            advisor_set = {
                "associatedChampions": [champion_key],
                "associatedMaps": [],
                "blocks": blocks,
                "map": "any",
                "mode": "any",
                "preferredItemSlots": [],
                "sortrank": 1,
                "startedFrom": "blank",
                "title": title,
                "type": "custom",
                "uid": stable_uid,
            }
            existing_sets = [
                item_set for item_set in (collection.get("itemSets") or [])
                if isinstance(item_set, dict)
                and item_set.get("uid") != stable_uid
                and item_set.get("title") != title
            ]
            collection["itemSets"] = [*existing_sets, advisor_set]
            collection["timestamp"] = int(time.time() * 1000)
            self.lcu.put(path, collection)
            early_order = ">".join(guide.skill_sequence[:4]) or "데이터 없음"
            master_order = ">".join(guide.skill_priority[:3]) or "데이터 없음"
            return (
                "Advisor 아이템 세트 적용 완료 · "
                f"초반 {early_order} · 마스터 {master_order}"
            )
        except LcuUnavailable as exc:
            raise BuildApplyError(str(exc)) from exc

    def apply_all(
        self, guide: ChampionBuildGuide, rune_build: RuneBuild,
        flash_slot: str = "F",
    ) -> list[str]:
        results: list[str] = []
        errors: list[str] = []
        for label, action in (
            ("룬", lambda: self.apply_runes(guide, rune_build)),
            ("스펠", lambda: self.apply_spells(guide, flash_slot)),
            ("아이템", lambda: self.apply_item_set(guide)),
        ):
            try:
                results.append(action())
            except BuildApplyError as exc:
                errors.append(f"{label}: {exc}")
        if not results:
            raise BuildApplyError(" / ".join(errors))
        if errors:
            results.append("일부 실패 · " + " / ".join(errors))
        return results
