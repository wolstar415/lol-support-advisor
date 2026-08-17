from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
import json
from pathlib import Path
import re
from typing import Any

from .lcu import LcuClient, LcuUnavailable
from .models import BuildAsset


RUNE_IMAGE_PREFIX = "/lol-game-data/assets/v1/"
RUNE_IMAGE_CDN = "https://ddragon.leagueoflegends.com/cdn/img/"


@dataclass(slots=True)
class RuneOption:
    perk_id: int
    name: str
    icon_path: str = ""
    short_desc: str = ""
    long_desc: str = ""
    style_id: int = 0
    slot_type: str = ""

    @property
    def icon_url(self) -> str:
        path = self.icon_path.replace("\\", "/")
        if path.startswith(RUNE_IMAGE_PREFIX):
            return f"{RUNE_IMAGE_CDN}{path.removeprefix(RUNE_IMAGE_PREFIX)}"
        return path if path.startswith(("http://", "https://")) else ""

    def as_asset(self) -> BuildAsset:
        return BuildAsset(self.perk_id, self.name, self.icon_url)


@dataclass(slots=True)
class RuneStyle:
    style_id: int
    name: str
    icon_path: str = ""
    tooltip: str = ""
    allowed_sub_styles: list[int] = field(default_factory=list)
    slots: list[list[int]] = field(default_factory=list)
    default_perks: list[int] = field(default_factory=list)

    @property
    def icon_url(self) -> str:
        path = self.icon_path.replace("\\", "/")
        if path.startswith(RUNE_IMAGE_PREFIX):
            return f"{RUNE_IMAGE_CDN}{path.removeprefix(RUNE_IMAGE_PREFIX)}"
        return path if path.startswith(("http://", "https://")) else ""


class RuneCatalog:
    """Patch-current Korean rune trees cached from the local League client."""

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.styles: dict[int, RuneStyle] = {}
        self.style_order: list[int] = []
        self.perks: dict[int, RuneOption] = {}
        self.updated_at = ""
        self.load()

    @property
    def ready(self) -> bool:
        return bool(self.styles and self.perks)

    @staticmethod
    def _plain_text(value: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.IGNORECASE)
        text = re.sub(r"</(?:li|p|maintext|stats|attention|passive|active)>", "\n", text,
                      flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        lines = [" ".join(line.split()) for line in unescape(text).splitlines()]
        return "\n".join(line for line in lines if line)

    def _apply(self, payload: dict[str, Any]) -> None:
        raw_styles = payload.get("styles") or []
        raw_perks = payload.get("perks") or []
        styles: dict[int, RuneStyle] = {}
        order: list[int] = []
        perks: dict[int, RuneOption] = {}
        for raw in raw_perks:
            if not isinstance(raw, dict) or not int(raw.get("id") or 0):
                continue
            perk_id = int(raw["id"])
            perks[perk_id] = RuneOption(
                perk_id=perk_id,
                name=str(raw.get("name") or f"룬 #{perk_id}"),
                icon_path=str(raw.get("iconPath") or ""),
                short_desc=str(raw.get("shortDesc") or ""),
                long_desc=str(raw.get("longDesc") or ""),
                style_id=int(raw.get("styleId") or 0),
                slot_type=str(raw.get("slotType") or ""),
            )
        for raw in raw_styles:
            if not isinstance(raw, dict) or not int(raw.get("id") or 0):
                continue
            style_id = int(raw["id"])
            slots = [
                [int(perk_id) for perk_id in (slot.get("perks") or [])]
                for slot in (raw.get("slots") or []) if isinstance(slot, dict)
            ]
            styles[style_id] = RuneStyle(
                style_id=style_id,
                name=str(raw.get("name") or f"계열 #{style_id}"),
                icon_path=str(raw.get("iconPath") or ""),
                tooltip=str(raw.get("tooltip") or ""),
                allowed_sub_styles=[int(value) for value in raw.get("allowedSubStyles") or []],
                slots=slots,
                default_perks=[int(value) for value in raw.get("defaultPerks") or []],
            )
            order.append(style_id)
        if styles and perks:
            self.styles = styles
            self.style_order = order
            self.perks = perks
            self.updated_at = str(payload.get("updatedAt") or "")

    def load(self) -> bool:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(payload, dict):
            return False
        self._apply(payload)
        return self.ready

    def refresh_from_lcu(self, lcu: LcuClient) -> int:
        styles = lcu.get("/lol-perks/v1/styles")
        perks = lcu.get("/lol-perks/v1/perks")
        if not isinstance(styles, list) or not isinstance(perks, list):
            raise LcuUnavailable("룬 계열 데이터를 읽지 못했습니다.")
        payload = {
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "styles": styles,
            "perks": perks,
        }
        self._apply(payload)
        if not self.ready:
            raise LcuUnavailable("룬 선택 데이터를 읽지 못했습니다.")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)
        return len(self.perks)

    def style(self, style_id: int) -> RuneStyle | None:
        return self.styles.get(int(style_id or 0))

    def perk(self, perk_id: int) -> RuneOption | None:
        return self.perks.get(int(perk_id or 0))

    def asset(self, perk_id: int, fallback: BuildAsset | None = None) -> BuildAsset:
        option = self.perk(perk_id)
        if option:
            return option.as_asset()
        return fallback or BuildAsset(int(perk_id), f"룬 #{perk_id}")

    def tooltip_text(self, perk_id: int, fallback_name: str = "") -> str:
        option = self.perk(perk_id)
        if not option:
            return f"{fallback_name or f'룬 #{perk_id}'}\n설명을 불러오는 중입니다."
        short = self._plain_text(option.short_desc)
        long = self._plain_text(option.long_desc)
        lines = [option.name]
        if short:
            lines.append(short)
        if long and long.casefold() != short.casefold():
            lines.append(long)
        return "\n".join(lines)

    def slot_index(self, style_id: int, perk_id: int, include_shards: bool = True) -> int | None:
        style = self.style(style_id)
        if not style:
            return None
        slots = style.slots if include_shards else style.slots[:4]
        return next(
            (index for index, values in enumerate(slots) if int(perk_id) in values),
            None,
        )
