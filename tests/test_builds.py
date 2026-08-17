from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.builds import BuildApplicator
from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.models import (
    BuildAsset, BuildItemGroup, ChampionBuildGuide, RuneBuild,
)


class FakeLcu:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str):
        self.calls.append(("GET", path, None))
        if path == "/lol-perks/v1/pages":
            return [{"id": 1, "name": "Blitz: Existing"}]
        if path == "/lol-summoner/v1/current-summoner":
            return {"summonerId": 123}
        if path == "/lol-item-sets/v1/item-sets/123/sets":
            return {
                "accountId": 456,
                "itemSets": [{"uid": "blitz", "title": "OP.GG Existing"}],
                "timestamp": 1,
            }
        raise AssertionError(path)

    def post(self, path: str, payload: object):
        self.calls.append(("POST", path, payload))

    def put(self, path: str, payload: object):
        self.calls.append(("PUT", path, payload))

    def patch(self, path: str, payload: object):
        self.calls.append(("PATCH", path, payload))


def guide() -> ChampionBuildGuide:
    return ChampionBuildGuide(
        champion_id="Thresh", champion_name_ko="쓰레쉬", position="SUPPORT",
        rune_builds=[RuneBuild(
            "추천 룬 1", 8400, 8300,
            [BuildAsset(perk_id, f"룬 {perk_id}") for perk_id in (
                8465, 8463, 8473, 8242, 8345, 8347, 5005, 5001, 5001,
            )],
        )],
        summoner_spells=[BuildAsset(4, "점멸"), BuildAsset(14, "점화")],
        skill_priority=["Q", "E", "W"],
        skill_sequence=list("QEWQ"),
        item_groups=[
            BuildItemGroup("시작 아이템", [
                BuildAsset(3865, "세계 지도"), BuildAsset(2003, "체력 물약"),
            ]),
            BuildItemGroup("핵심 아이템", [BuildAsset(3190, "솔라리")]),
        ],
    )


class BuildApplicatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.registry = ChampionRegistry(Path(self.temp_dir.name) / "champions.json")
        self.lcu = FakeLcu()
        self.applicator = BuildApplicator(self.lcu, self.registry)  # type: ignore[arg-type]
        self.guide = guide()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_runes_create_only_advisor_page(self) -> None:
        self.applicator.apply_runes(self.guide, self.guide.rune_builds[0])
        method, path, payload = self.lcu.calls[-1]
        self.assertEqual((method, path), ("POST", "/lol-perks/v1/pages"))
        self.assertEqual(payload["selectedPerkIds"][0], 8465)  # type: ignore[index]
        self.assertIn("LOL Advisor", payload["name"])  # type: ignore[index]

    def test_spells_patch_only_my_selection(self) -> None:
        self.applicator.apply_spells(self.guide)
        self.assertEqual(
            self.lcu.calls[-1],
            (
                "PATCH", "/lol-champ-select/v1/session/my-selection",
                {"spell1Id": 14, "spell2Id": 4},
            ),
        )

    def test_flash_can_be_kept_on_d(self) -> None:
        self.applicator.apply_spells(self.guide, "D")
        self.assertEqual(
            self.lcu.calls[-1][2], {"spell1Id": 4, "spell2Id": 14}
        )

    def test_item_set_preserves_existing_sets(self) -> None:
        self.applicator.apply_item_set(self.guide)
        method, path, payload = self.lcu.calls[-1]
        self.assertEqual((method, path), (
            "PUT", "/lol-item-sets/v1/item-sets/123/sets"
        ))
        item_sets = payload["itemSets"]  # type: ignore[index]
        self.assertEqual(item_sets[0]["uid"], "blitz")
        self.assertIn("LOL Advisor", item_sets[1]["title"])
        self.assertEqual(item_sets[1]["associatedChampions"], [412])
        blocks = item_sets[1]["blocks"]
        self.assertEqual(
            blocks[0]["type"],
            "[Advisor] 시작 아이템 · 초반 스킬 순서 Q>E>W>Q",
        )
        self.assertEqual(
            blocks[1]["type"],
            "[Advisor] 소모품 · 스킬 마스터 순서 Q>E>W",
        )
        self.assertEqual(blocks[2]["type"], "[Advisor] 핵심 아이템 빌드")
        self.assertEqual(blocks[-1]["type"], "[Advisor] 장신구")
        self.assertEqual(
            [item["id"] for item in blocks[1]["items"]], ["2003", "2055"]
        )


if __name__ == "__main__":
    unittest.main()
