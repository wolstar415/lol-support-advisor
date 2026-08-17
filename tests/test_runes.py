from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lol_support_advisor.runes import RuneCatalog


class RuneCatalogTests(unittest.TestCase):
    def _payload(self) -> dict:
        return {
            "updatedAt": "2026-08-17T22:00:00",
            "styles": [
                {
                    "id": 8400,
                    "name": "결의",
                    "iconPath": "/lol-game-data/assets/v1/perk-images/Styles/7204_Resolve.png",
                    "tooltip": "내구력",
                    "allowedSubStyles": [8300],
                    "defaultPerks": [8465, 0, 0, 0, 0, 0, 5008, 5008, 5001],
                    "slots": [
                        {"perks": [8465]},
                        {"perks": [8463]},
                        {"perks": [8473]},
                        {"perks": [8242]},
                        {"perks": [5008, 5005]},
                        {"perks": [5008, 5001]},
                        {"perks": [5011, 5001]},
                    ],
                }
            ],
            "perks": [
                {
                    "id": 8465,
                    "name": "수호자",
                    "iconPath": "/lol-game-data/assets/v1/perk-images/Styles/Resolve/Guardian/Guardian.png",
                    "shortDesc": "아군을 <b>보호</b>합니다.",
                    "longDesc": "아군에게 보호막을 제공합니다.<br>재사용 대기시간이 있습니다.",
                    "styleId": 8400,
                    "slotType": "kKeyStone",
                }
            ],
        }

    def test_cached_tree_and_slot_lookup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runes.json"
            path.write_text(json.dumps(self._payload(), ensure_ascii=False), encoding="utf-8")
            catalog = RuneCatalog(path)
            self.assertTrue(catalog.ready)
            self.assertEqual(catalog.style(8400).slots[0], [8465])
            self.assertEqual(catalog.slot_index(8400, 8465), 0)

    def test_korean_tooltip_and_cdn_icon_url(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runes.json"
            path.write_text(json.dumps(self._payload(), ensure_ascii=False), encoding="utf-8")
            catalog = RuneCatalog(path)
            tooltip = catalog.tooltip_text(8465)
            self.assertIn("수호자", tooltip)
            self.assertIn("아군을 보호합니다.", tooltip)
            self.assertNotIn("<b>", tooltip)
            self.assertEqual(
                catalog.perk(8465).icon_url,
                "https://ddragon.leagueoflegends.com/cdn/img/"
                "perk-images/Styles/Resolve/Guardian/Guardian.png",
            )


if __name__ == "__main__":
    unittest.main()

