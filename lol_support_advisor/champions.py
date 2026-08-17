from __future__ import annotations

import json
from pathlib import Path
import ssl
from typing import Any
from urllib.request import Request, urlopen


FALLBACK_CHAMPIONS: dict[int, tuple[str, str]] = {
    12: ("Alistar", "알리스타"), 53: ("Blitzcrank", "블리츠크랭크"),
    432: ("Bard", "바드"), 201: ("Braum", "브라움"), 63: ("Brand", "브랜드"),
    40: ("Janna", "잔나"), 43: ("Karma", "카르마"), 89: ("Leona", "레오나"),
    99: ("Lux", "럭스"), 117: ("Lulu", "룰루"), 902: ("Milio", "밀리오"),
    25: ("Morgana", "모르가나"), 267: ("Nami", "나미"), 111: ("Nautilus", "노틸러스"),
    555: ("Pyke", "파이크"), 497: ("Rakan", "라칸"), 526: ("Rell", "렐"),
    888: ("Renata", "레나타 글라스크"), 235: ("Senna", "세나"),
    147: ("Seraphine", "세라핀"), 37: ("Sona", "소나"), 16: ("Soraka", "소라카"),
    223: ("TahmKench", "탐 켄치"), 44: ("Taric", "타릭"), 412: ("Thresh", "쓰레쉬"),
    350: ("Yuumi", "유미"), 26: ("Zilean", "질리언"), 143: ("Zyra", "자이라"),
    22: ("Ashe", "애쉬"), 9: ("Fiddlesticks", "피들스틱"), 74: ("Heimerdinger", "하이머딩거"),
    57: ("Maokai", "마오카이"), 518: ("Neeko", "니코"), 78: ("Poppy", "뽀삐"),
    35: ("Shaco", "샤코"), 161: ("Velkoz", "벨코즈"), 101: ("Xerath", "제라스"),
    266: ("Aatrox", "아트록스"), 122: ("Darius", "다리우스"), 120: ("Hecarim", "헤카림"),
    222: ("Jinx", "징크스"), 145: ("Kaisa", "카이사"), 55: ("Katarina", "카타리나"),
    64: ("LeeSin", "리 신"), 54: ("Malphite", "말파이트"), 516: ("Ornn", "오른"),
    360: ("Samira", "사미라"), 234: ("Viego", "비에고"), 157: ("Yasuo", "야스오"),
    238: ("Zed", "제드"), 134: ("Syndra", "신드라"), 76: ("Nidalee", "니달리"),
}

DEDICATED_SUPPORTS = {
    "Alistar", "Bard", "Blitzcrank", "Braum", "Janna", "Karma", "Leona", "Lulu",
    "Milio", "Morgana", "Nami", "Nautilus", "Pyke", "Rakan", "Rell", "Renata",
    "Senna", "Seraphine", "Sona", "Soraka", "TahmKench", "Taric", "Thresh", "Yuumi",
    "Zilean", "Zyra",
}

POSSIBLE_SUPPORTS = DEDICATED_SUPPORTS | {
    "Ashe", "Brand", "Fiddlesticks", "Heimerdinger", "Lux", "Maokai", "Neeko",
    "Poppy", "Shaco", "Velkoz", "Xerath",
}


class ChampionRegistry:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.by_key: dict[int, tuple[str, str]] = dict(FALLBACK_CHAMPIONS)
        self.by_id: dict[str, tuple[int, str]] = {
            champion_id: (key, ko_name) for key, (champion_id, ko_name) in self.by_key.items()
        }
        self.loaded_from_ddragon = False
        self.version = "fallback"
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._apply_ddragon(data)
        except (OSError, ValueError, KeyError, TypeError):
            return

    def refresh(self, timeout: float = 10.0) -> int:
        context = ssl.create_default_context()
        versions_request = Request(
            "https://ddragon.leagueoflegends.com/api/versions.json",
            headers={"User-Agent": "LOL-Support-Advisor/0.1"},
        )
        with urlopen(versions_request, timeout=timeout, context=context) as response:
            version = json.loads(response.read().decode("utf-8"))[0]
        data_request = Request(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/ko_KR/champion.json",
            headers={"User-Agent": "LOL-Support-Advisor/0.1"},
        )
        with urlopen(data_request, timeout=timeout, context=context) as response:
            data = json.loads(response.read().decode("utf-8"))
        data["_advisor_version"] = version
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self._apply_ddragon(data)
        return len(self.by_key)

    def _apply_ddragon(self, payload: dict[str, Any]) -> None:
        champion_data = payload["data"]
        parsed: dict[int, tuple[str, str]] = {}
        for item in champion_data.values():
            parsed[int(item["key"])] = (str(item["id"]), str(item["name"]))
        if not parsed:
            return
        self.by_key = parsed
        self.by_id = {champion_id: (key, name) for key, (champion_id, name) in parsed.items()}
        self.loaded_from_ddragon = True
        self.version = str(payload.get("_advisor_version", "cached"))

    def from_key(self, key: int | str | None) -> tuple[str, str]:
        try:
            numeric = int(key or 0)
        except (TypeError, ValueError):
            numeric = 0
        return self.by_key.get(numeric, (f"Champion{numeric}", f"챔피언 #{numeric}"))

    def ko_name(self, champion_id: str | None) -> str:
        if not champion_id:
            return "미정"
        return self.by_id.get(champion_id, (0, champion_id))[1]

    def contains(self, champion_id: str) -> bool:
        return champion_id in self.by_id or champion_id.startswith("Champion")

    @staticmethod
    def _normalized(value: str) -> str:
        return "".join(character for character in value.casefold() if character.isalnum())

    def normalize_id(self, value: str | None) -> str:
        if not value:
            return "Unknown"
        if value in self.by_id:
            return value
        normalized = self._normalized(value)
        for champion_id, (_key, name_ko) in self.by_id.items():
            if normalized in {self._normalized(champion_id), self._normalized(name_ko)}:
                return champion_id
        aliases = {
            "renataglasc": "Renata", "wukong": "MonkeyKing", "nunuandwillump": "Nunu",
        }
        return aliases.get(normalized, value.replace("'", "").replace(" ", ""))

    def icon_url(self, champion_id: str) -> str | None:
        if self.version == "fallback":
            return None
        return (
            f"https://ddragon.leagueoflegends.com/cdn/{self.version}/img/champion/"
            f"{champion_id}.png"
        )

    def slug(self, champion_id: str) -> str:
        # OP.GG keeps Riot's internal champion id in Wukong's canonical URL.
        special = {"Fiddlesticks": "fiddlesticks", "MonkeyKing": "monkeyking"}
        return special.get(champion_id, champion_id.lower().replace("'", "").replace(" ", ""))

    def support_score(self, champion_id: str) -> int:
        if champion_id in DEDICATED_SUPPORTS:
            return 2
        if champion_id in POSSIBLE_SUPPORTS:
            return 1
        return 0
