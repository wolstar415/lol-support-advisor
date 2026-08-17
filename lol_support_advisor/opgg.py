from __future__ import annotations

from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import json
import re
import ssl
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .champions import ChampionRegistry, POSSIBLE_SUPPORTS
from .models import (
    BuildAsset, BuildItemGroup, ChampionBuildGuide, OpggCounter,
    OpggPlayerChampionStat, OpggSnapshot, RuneBuild,
)


POSITION_TO_OPGG = {
    "TOP": "top",
    "JUNGLE": "jungle",
    "MIDDLE": "mid",
    "BOTTOM": "adc",
    "SUPPORT": "support",
    "UTILITY": "support",
}

POSITION_KO = {
    "TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드",
    "BOTTOM": "원딜", "SUPPORT": "서포터", "UTILITY": "서포터",
}

SPELL_IDS = {
    "cleanse": 1, "exhaust": 3, "flash": 4, "ghost": 6, "heal": 7,
    "smite": 11, "teleport": 12, "ignite": 14, "barrier": 21,
}

ITEM_GROUP_NAMES = {
    "starter items": "시작 아이템",
    "boots": "신발",
    "support items": "서포터 퀘스트 완성",
    "core builds": "핵심 아이템",
    "core items": "핵심 아이템",
    "core final build": "완성 빌드",
    "final build": "완성 빌드",
    "full build": "완성 빌드",
    "item builds": "완성 빌드",
    "fourth item": "4번째 아이템",
    "fifth item": "5번째 아이템",
    "sixth item": "6번째 아이템",
    "situational items": "상황별 아이템",
}


class OpggError(RuntimeError):
    pass


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if value:
            self.tokens.append(value)


class OpggClient:
    """Best-effort, low-frequency reader for OP.GG's public champion pages.

    OP.GG does not expose a public aggregate-data API. The parser deliberately
    fails closed when page markup changes, so stale or invented numbers never
    reach recommendations.
    """

    def __init__(self, registry: ChampionRegistry, timeout: float = 15.0) -> None:
        self.registry = registry
        self.timeout = timeout

    def _fetch(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": "LOL-Support-Advisor/0.1 (personal use; source: OP.GG)",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout, context=ssl.create_default_context()) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raise OpggError(f"OP.GG 응답 오류: HTTP {exc.code}") from exc
        except (URLError, OSError) as exc:
            raise OpggError(f"OP.GG 연결 실패: {exc}") from exc

    @staticmethod
    def _number(token: str) -> float | None:
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", token)
        return float(match.group(1)) if match else None

    @staticmethod
    def _games(token: str) -> int | None:
        if "%" in token:
            return None
        match = re.fullmatch(r"\s*([\d,]+)\s*", token)
        if not match:
            return None
        return int(match.group(1).replace(",", ""))

    def _champion_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for champion_id, (_, name_ko) in self.registry.by_id.items():
            aliases[champion_id.casefold()] = champion_id
            aliases[name_ko.casefold()] = champion_id
            aliases[champion_id.replace("'", "").replace(" ", "").casefold()] = champion_id
        aliases.update(
            {
                "renataglasc": "Renata",
                "tahmkench": "TahmKench",
                "velkoz": "Velkoz",
                "blitzcrank": "Blitzcrank",
                "wukong": "MonkeyKing",
                "nunuandwillump": "Nunu",
            }
        )
        return aliases

    @staticmethod
    def _position(position: str) -> str:
        normalized = str(position or "SUPPORT").upper()
        return normalized if normalized in POSITION_TO_OPGG else "SUPPORT"

    def _allowed_candidates(self, position: str) -> set[str]:
        if self._position(position) == "SUPPORT":
            return set(POSSIBLE_SUPPORTS)
        # The counter/position page itself is the authority for non-support
        # lanes. Keeping all registered champions avoids a stale hard-coded
        # lane list when Riot adds or flexes a champion.
        return set(self.registry.by_id)

    def _table_entries(
        self, tokens: list[str], target_id: str, position: str = "SUPPORT"
    ) -> list[OpggCounter]:
        aliases = self._champion_aliases()
        allowed = self._allowed_candidates(position)
        entries: dict[str, OpggCounter] = {}
        table_start = 0
        for index in range(len(tokens) - 1):
            if tokens[index].casefold() == "win rate" and tokens[index + 1].casefold() == "games":
                table_start = index + 2
        # Champion rows are rendered as: champion name, win rate, games. We scan
        # a short window because extra accessibility labels may appear between them.
        for index, token in enumerate(tokens[table_start:], start=table_start):
            normalized = token.replace("'", "").replace(" ", "").casefold()
            champion_id = aliases.get(token.casefold()) or aliases.get(normalized)
            if not champion_id or champion_id == target_id or champion_id not in allowed:
                continue
            rate: float | None = None
            rate_index: int | None = None
            games: int | None = None
            for nearby_index in range(index + 1, min(index + 8, len(tokens))):
                nearby = tokens[nearby_index]
                if rate is None:
                    rate = self._number(nearby)
                    if rate is None and re.fullmatch(r"\d+(?:\.\d+)?", nearby):
                        if nearby_index + 1 < len(tokens) and tokens[nearby_index + 1] == "%":
                            rate = float(nearby)
                    if rate is not None:
                        rate_index = nearby_index
                        continue
                if rate is not None and rate_index is not None and nearby_index > rate_index:
                    games = self._games(nearby)
                    if games is not None:
                        break
            if rate is None or games is None or not (0 <= rate <= 100):
                continue
            candidate_rate = round(100.0 - rate, 2)
            current = entries.get(champion_id)
            if current is None or games > current.games:
                entries[champion_id] = OpggCounter(
                    champion_id=champion_id,
                    champion_name_ko=self.registry.ko_name(champion_id),
                    versus_win_rate=candidate_rate,
                    games=games,
                )
        return list(entries.values())

    @staticmethod
    def _find_rate_after_label(tokens: Iterable[str], label: str) -> float | None:
        values = list(tokens)
        for index, token in enumerate(values):
            if token.casefold() != label.casefold():
                continue
            for nearby in values[index + 1:index + 4]:
                match = re.search(r"(\d+(?:\.\d+)?)\s*%", nearby)
                if match:
                    return float(match.group(1))
            for nearby_index in range(index + 1, min(index + 4, len(values) - 1)):
                if re.fullmatch(r"\d+(?:\.\d+)?", values[nearby_index]) \
                        and values[nearby_index + 1] == "%":
                    return float(values[nearby_index])
        return None

    def refresh_matchup(
        self, enemy_support_id: str, position: str = "SUPPORT"
    ) -> OpggSnapshot:
        position = self._position(position)
        position_slug = POSITION_TO_OPGG[position]
        slug = self.registry.slug(enemy_support_id)
        url = (
            f"https://op.gg/lol/champions/{slug}/counters/{position_slug}"
            "?region=global&tier=emerald_plus&type=ranked"
        )
        html = self._fetch(url)
        parser = _VisibleTextParser()
        parser.feed(html)
        tokens = parser.tokens
        entries = self._table_entries(tokens, enemy_support_id, position)
        joined = " ".join(tokens)
        insufficient_sample = (
            "sample size is not large enough" in joined.casefold()
        )
        if not entries and not insufficient_sample:
            raise OpggError(
                "OP.GG 페이지에서 카운터 표를 읽지 못했습니다. 페이지 형식이 변경되었을 수 있습니다."
            )
        # Keep the complete table in cache. UI archetype filters are applied
        # afterwards; truncating first can otherwise hide every poke champion.
        counters = sorted((entry for entry in entries if entry.versus_win_rate >= 50),
                          key=lambda item: (item.versus_win_rate, item.games), reverse=True)
        weak = sorted((entry for entry in entries if entry.versus_win_rate < 50),
                      key=lambda item: (item.versus_win_rate, -item.games), reverse=True)
        patch_match = re.search(r"Patch\s+(\d+\.\d+)", joined, flags=re.IGNORECASE)
        snapshot = OpggSnapshot(
            enemy_support_id=enemy_support_id,
            enemy_support_name_ko=self.registry.ko_name(enemy_support_id),
            position=position,
            region="GLOBAL",
            tier="EMERALD_PLUS",
            patch=patch_match.group(1) if patch_match else "UNKNOWN",
            updated_at=datetime.now().isoformat(timespec="seconds"),
            source_url=url,
            counters=counters,
            weak_picks=weak,
            target_overall_win_rate=self._find_rate_after_label(tokens, "Win rate"),
            target_pick_rate=self._find_rate_after_label(tokens, "Pick rate"),
            target_ban_rate=self._find_rate_after_label(tokens, "Ban rate"),
            raw_status="OK" if entries else "NO_DATA",
        )
        return snapshot

    def parse_summoner_champion_page(
        self, html: str, champion_id: str, source_url: str = ""
    ) -> OpggPlayerChampionStat:
        parser = _VisibleTextParser()
        parser.feed(html)
        tokens = parser.tokens
        table_start = next(
            (
                index + 3 for index in range(len(tokens) - 2)
                if tokens[index:index + 3] == ["#", "Champion", "Played"]
            ),
            None,
        )
        if table_start is None:
            raise OpggError(
                "OP.GG 소환사 챔피언 표를 읽지 못했습니다. 페이지 형식이 변경되었을 수 있습니다."
            )

        page_updated = ""
        for index, token in enumerate(tokens[:table_start]):
            if token.casefold() != "last updated":
                continue
            nearby = [value for value in tokens[index + 1:index + 4] if value != ":"]
            if nearby:
                page_updated = nearby[0]
            break

        aliases = self._champion_aliases()
        candidates: list[tuple[int, int]] = []
        for index in range(table_start, len(tokens) - 6):
            token = tokens[index]
            normalized = token.replace("'", "").replace(" ", "").casefold()
            parsed_id = aliases.get(token.casefold()) or aliases.get(normalized)
            if parsed_id != champion_id or index <= 0:
                continue
            rank = self._games(tokens[index - 1])
            if rank is None or not 1 <= rank <= len(self.registry.by_id):
                continue
            wins = self._games(tokens[index + 1])
            losses = self._games(tokens[index + 3])
            if (
                wins is None or losses is None
                or tokens[index + 2].upper() != "W"
                or tokens[index + 4].upper() != "L"
                or self._number(tokens[index + 5]) is None
            ):
                continue
            candidates.append((wins, losses))

        if candidates:
            wins, losses = max(candidates, key=lambda item: item[0] + item[1])
            status = "OK"
        else:
            wins = losses = 0
            status = "NO_DATA"
        return OpggPlayerChampionStat(
            champion_id=champion_id,
            champion_name_ko=self.registry.ko_name(champion_id),
            wins=wins,
            losses=losses,
            page_updated_text=page_updated,
            fetched_at=datetime.now().isoformat(timespec="seconds"),
            source_url=source_url,
            status=status,
        )

    def refresh_summoner_champion(
        self, game_name: str, tag_line: str, champion_id: str
    ) -> OpggPlayerChampionStat:
        account_slug = (
            f"{quote(game_name.strip(), safe='')}-{quote(tag_line.strip(), safe='')}"
        )
        url = f"https://op.gg/lol/summoners/kr/{account_slug}/champions"
        return self.parse_summoner_champion_page(
            self._fetch(url), champion_id, source_url=url
        )

    def _position_entries(
        self, position: str = "SUPPORT"
    ) -> tuple[str, str, list[OpggCounter]]:
        position = self._position(position)
        position_slug = POSITION_TO_OPGG[position]
        url = f"https://op.gg/lol/champions?position={position_slug}&tier=emerald_plus"
        html = self._fetch(url)
        parser = _VisibleTextParser()
        parser.feed(html)
        tokens = parser.tokens
        aliases = self._champion_aliases()
        # The position ranking page itself is authoritative. This deliberately
        # avoids a hard-coded support list hiding newly released or flex picks.
        allowed = set(self.registry.by_id)
        entries: dict[str, OpggCounter] = {}
        table_start = next(
            (index for index, token in enumerate(tokens) if token.casefold() == "ranking table"),
            0,
        )
        for index, token in enumerate(tokens[table_start:], start=table_start):
            normalized = token.replace("'", "").replace(" ", "").casefold()
            champion_id = aliases.get(token.casefold()) or aliases.get(normalized)
            if not champion_id or champion_id not in allowed or champion_id in entries:
                continue
            percentages: list[float] = []
            nearby_tokens = tokens[index + 1:index + 12]
            for nearby_index, nearby in enumerate(nearby_tokens):
                rate = self._number(nearby)
                if rate is None and re.fullmatch(r"\d+(?:\.\d+)?", nearby):
                    if nearby_index + 1 < len(nearby_tokens) and nearby_tokens[nearby_index + 1] == "%":
                        rate = float(nearby)
                if rate is not None:
                    percentages.append(rate)
                if len(percentages) >= 3:
                    break
            if len(percentages) < 3 or not (35 <= percentages[0] <= 65):
                continue
            previous_percent = next(
                (
                    token_index for token_index in range(index - 1, table_start - 1, -1)
                    if tokens[token_index] == "%"
                ),
                table_start,
            )
            rank = next(
                (
                    int(value) for value in tokens[previous_percent + 1:index]
                    if re.fullmatch(r"\d{1,3}", value) and 1 <= int(value) <= 200
                ),
                len(entries) + 1,
            )
            entries[champion_id] = OpggCounter(
                champion_id=champion_id,
                champion_name_ko=self.registry.ko_name(champion_id),
                versus_win_rate=percentages[0],
                overall_win_rate=percentages[0],
                games=0,
                position_rank=rank,
                pick_rate=percentages[1],
                ban_rate=percentages[2],
            )
        if len(entries) < 3:
            raise OpggError(
                f"OP.GG 페이지에서 {POSITION_KO[position]} 통계 표를 읽지 못했습니다. "
                "페이지 형식이 변경되었을 수 있습니다."
            )
        joined = " ".join(tokens)
        patch_match = re.search(r"Patch\s+(\d+\.\d+)", joined, flags=re.IGNORECASE)
        patch = patch_match.group(1) if patch_match else "UNKNOWN"
        candidates = sorted(entries.values(), key=lambda item: item.position_rank or 999)
        return url, patch, candidates

    def refresh_position_champions(
        self, position: str = "SUPPORT"
    ) -> tuple[str, list[str]]:
        """Return every champion present in OP.GG's current position ranking."""
        _url, patch, entries = self._position_entries(position)
        return patch, [entry.champion_id for entry in entries]

    def refresh_overall(self, position: str = "SUPPORT") -> OpggSnapshot:
        position = self._position(position)
        url, patch, candidates = self._position_entries(position)
        return OpggSnapshot(
            enemy_support_id=None,
            enemy_support_name_ko=None,
            position=position,
            region="GLOBAL",
            tier="EMERALD_PLUS",
            patch=patch,
            updated_at=datetime.now().isoformat(timespec="seconds"),
            source_url=url,
            counters=candidates[:30],
            raw_status="OK",
        )

    @staticmethod
    def _html_tables(html: str) -> list[str]:
        return re.findall(r"<table\b.*?</table>", html, flags=re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _table_tokens(table_html: str) -> list[str]:
        parser = _VisibleTextParser()
        parser.feed(table_html)
        return parser.tokens

    @staticmethod
    def _table_images(table_html: str) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for tag in re.findall(r"<img\b[^>]*>", table_html, flags=re.IGNORECASE):
            alt_match = re.search(r'\balt="([^"]*)"', tag, flags=re.IGNORECASE)
            src_match = re.search(r'\bsrc="([^"]*)"', tag, flags=re.IGNORECASE)
            if src_match:
                result.append((
                    unescape(alt_match.group(1) if alt_match else ""),
                    unescape(src_match.group(1)),
                ))
        return result

    @staticmethod
    def _escaped_rune_assets(html: str) -> dict[int, BuildAsset]:
        assets: dict[int, BuildAsset] = {}
        pattern = re.compile(
            r'\\"id\\":(\d+),\\"name\\":\\"([^"\\]*(?:\\.[^"\\]*)*)\\",'
            r'\\"image_url\\":\\"([^"\\]*(?:\\.[^"\\]*)*)\\"'
        )
        for match in pattern.finditer(html):
            asset_id = int(match.group(1))
            try:
                name = json.loads(f'"{match.group(2)}"')
                icon_url = json.loads(f'"{match.group(3)}"')
            except ValueError:
                name = match.group(2)
                icon_url = match.group(3).replace("\\/", "/")
            assets.setdefault(asset_id, BuildAsset(asset_id, str(name), str(icon_url)))
        return assets

    def refresh_build(
        self, champion_id: str, position: str = "SUPPORT"
    ) -> ChampionBuildGuide:
        position = self._position(position)
        position_slug = POSITION_TO_OPGG[position]
        slug = self.registry.slug(champion_id)
        url = (
            f"https://op.gg/lol/champions/{slug}/build/{position_slug}"
            "?region=global&tier=emerald_plus&type=ranked"
        )
        html = self._fetch(url)
        joined_parser = _VisibleTextParser()
        joined_parser.feed(html)
        joined = " ".join(joined_parser.tokens)
        patch_match = re.search(r"Patch\s+(\d+\.\d+)", joined, flags=re.IGNORECASE)
        if not patch_match:
            patch_match = re.search(r"/lol/(\d+\.\d+)\.\d+/", html)
        patch = patch_match.group(1) if patch_match else "UNKNOWN"

        rune_assets = self._escaped_rune_assets(html)
        rune_pattern = re.compile(
            r'\\"importClientData\\":\{\\"type\\":\\"CHAMPION_DETAIL_BUILD\\",'
            r'\\"championKey\\":\\"[^"\\]+\\",\\"primaryStyleId\\":(\d+),'
            r'\\"subStyleId\\":(\d+),\\"selectedPerkIds\\":\[([\d,]+)\]\}'
        )
        rune_builds: list[RuneBuild] = []
        seen_perks: set[tuple[int, ...]] = set()
        for match in rune_pattern.finditer(html):
            perk_ids = tuple(int(value) for value in match.group(3).split(",") if value)
            if len(perk_ids) != 9 or perk_ids in seen_perks:
                continue
            seen_perks.add(perk_ids)
            perks = [
                rune_assets.get(
                    perk_id,
                    BuildAsset(
                        perk_id,
                        f"룬 {perk_id}",
                        f"https://opgg-static.akamaized.net/meta/images/lol/"
                        f"{patch}.1/perk/{perk_id}.png",
                    ),
                )
                for perk_id in perk_ids
            ]
            rune_builds.append(RuneBuild(
                name=f"추천 룬 {len(rune_builds) + 1}",
                primary_style_id=int(match.group(1)),
                sub_style_id=int(match.group(2)),
                perks=perks,
            ))
            if len(rune_builds) >= 3:
                break

        tables = self._html_tables(html)
        spells: list[BuildAsset] = []
        skill_priority: list[str] = []
        skill_sequence: list[str] = []
        item_groups: list[BuildItemGroup] = []
        seen_item_groups: set[tuple[int, ...]] = set()
        for table in tables:
            tokens = self._table_tokens(table)
            token_text = " ".join(tokens)
            images = self._table_images(table)
            if "SummonerSpells Table" in token_text and not spells:
                for name, icon_url in images:
                    spell_id = SPELL_IDS.get(name.casefold())
                    if spell_id and all(spell.asset_id != spell_id for spell in spells):
                        spells.append(BuildAsset(spell_id, name, icon_url))
                    if len(spells) == 2:
                        break
            elif "SkillOrder Table" in token_text and not skill_priority:
                letters = [token for token in tokens if token in {"Q", "W", "E", "R"}]
                if len(letters) >= 3:
                    skill_priority = letters[:3]
                    skill_sequence = letters[3:21]
            elif any("/item/" in source for _name, source in images):
                items: list[BuildAsset] = []
                seen_item_ids: set[int] = set()
                for name, icon_url in images:
                    item_match = re.search(r"/item/(\d+)\.png", icon_url)
                    if not item_match:
                        continue
                    item_id = int(item_match.group(1))
                    if item_id in seen_item_ids and "Starter items" not in token_text:
                        continue
                    seen_item_ids.add(item_id)
                    items.append(BuildAsset(item_id, name or f"아이템 {item_id}", icon_url))
                item_signature = tuple(item.asset_id for item in items)
                if not items or item_signature in seen_item_groups:
                    continue
                seen_item_groups.add(item_signature)
                lowered = [token.casefold() for token in tokens]
                english_title = next(
                    (title for title in ITEM_GROUP_NAMES if title in lowered),
                    "",
                )
                title = ITEM_GROUP_NAMES.get(
                    english_title, f"추천 아이템 {len(item_groups) + 1}"
                )
                if position == "SUPPORT" and title == "시작 아이템" \
                        and all(item.asset_id != 3865 for item in items):
                    items.insert(0, BuildAsset(
                        3865, "World Atlas",
                        f"https://opgg-static.akamaized.net/meta/images/lol/"
                        f"{patch}.1/item/3865.png",
                    ))
                item_groups.append(BuildItemGroup(title, items))

        if not rune_builds or len(spells) < 2 or not item_groups:
            raise OpggError(
                "OP.GG 빌드 페이지에서 룬·스펠·아이템을 완전하게 읽지 못했습니다. "
                "페이지 형식이 변경되었을 수 있습니다."
            )
        return ChampionBuildGuide(
            champion_id=champion_id,
            champion_name_ko=self.registry.ko_name(champion_id),
            position=position,
            patch=patch,
            tier="EMERALD_PLUS",
            updated_at=datetime.now().isoformat(timespec="seconds"),
            source_url=url,
            rune_builds=rune_builds,
            summoner_spells=spells,
            skill_priority=skill_priority,
            skill_sequence=skill_sequence,
            item_groups=item_groups[:14],
        )
