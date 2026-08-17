from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
import re
import ssl
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .champions import ChampionRegistry, POSSIBLE_SUPPORTS
from .models import OpggCounter, OpggSnapshot


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
        if len(entries) < 2:
            raise OpggError(
                "OP.GG 페이지에서 카운터 표를 읽지 못했습니다. 페이지 형식이 변경되었을 수 있습니다."
            )
        counters = sorted((entry for entry in entries if entry.versus_win_rate >= 50),
                          key=lambda item: (item.versus_win_rate, item.games), reverse=True)[:10]
        weak = sorted((entry for entry in entries if entry.versus_win_rate < 50),
                      key=lambda item: (item.versus_win_rate, -item.games))[:5]
        joined = " ".join(tokens)
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
            raw_status="OK",
        )
        return snapshot

    def refresh_overall(self, position: str = "SUPPORT") -> OpggSnapshot:
        position = self._position(position)
        position_slug = POSITION_TO_OPGG[position]
        url = f"https://op.gg/lol/champions?position={position_slug}&tier=emerald_plus"
        html = self._fetch(url)
        parser = _VisibleTextParser()
        parser.feed(html)
        tokens = parser.tokens
        aliases = self._champion_aliases()
        allowed = self._allowed_candidates(position)
        entries: dict[str, OpggCounter] = {}
        for index, token in enumerate(tokens):
            normalized = token.replace("'", "").replace(" ", "").casefold()
            champion_id = aliases.get(token.casefold()) or aliases.get(normalized)
            if not champion_id or champion_id not in allowed:
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
            if not percentages or not (35 <= percentages[0] <= 65):
                continue
            entries[champion_id] = OpggCounter(
                champion_id=champion_id,
                champion_name_ko=self.registry.ko_name(champion_id),
                versus_win_rate=percentages[0],
                overall_win_rate=percentages[0],
                games=0,
            )
        if len(entries) < 3:
            raise OpggError(
                f"OP.GG 페이지에서 {POSITION_KO[position]} 통계 표를 읽지 못했습니다. "
                "페이지 형식이 변경되었을 수 있습니다."
            )
        joined = " ".join(tokens)
        patch_match = re.search(r"Patch\s+(\d+\.\d+)", joined, flags=re.IGNORECASE)
        candidates = sorted(entries.values(), key=lambda item: item.overall_win_rate or 0, reverse=True)[:15]
        return OpggSnapshot(
            enemy_support_id=None,
            enemy_support_name_ko=None,
            position=position,
            region="GLOBAL",
            tier="EMERALD_PLUS",
            patch=patch_match.group(1) if patch_match else "UNKNOWN",
            updated_at=datetime.now().isoformat(timespec="seconds"),
            source_url=url,
            counters=candidates,
            raw_status="OK",
        )
