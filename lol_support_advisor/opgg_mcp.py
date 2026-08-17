from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import (
    OpggMcpChampionStat, OpggMcpRecentMatch, OpggMcpSummonerProfile,
    OpggSynergySnapshot, OpggSynergyStat,
)


OPGG_MCP_ENDPOINT = "https://mcp-api.op.gg/mcp"
_JSON_STRING = r'"(?:\\.|[^"\\])*"'


class OpggMcpError(RuntimeError):
    pass


def _decoded_token(value: str) -> Any:
    token = value.strip()
    if token in {"null", "None", ""}:
        return None
    if token.startswith('"'):
        try:
            return json.loads(token)
        except (TypeError, ValueError):
            return token.strip('"')
    try:
        return int(token)
    except ValueError:
        return token


def parse_summoner_profile_text(
    text: str,
    *,
    requested_game_name: str,
    requested_tag_line: str,
    region: str = "KR",
    fetched_at: str | None = None,
) -> OpggMcpSummonerProfile:
    """Parse OP.GG MCP's compact constructor-style text response.

    The public MCP currently returns TextContent rather than structuredContent.
    We request a closed, minimal field set and parse only those constructors so
    unrelated additions to the response do not affect the live card cache.
    """
    if not text or "LolGetSummonerProfile" not in text:
        raise OpggMcpError("OP.GG MCP 소환사 응답 형식을 확인할 수 없습니다.")

    game_name = requested_game_name
    source_updated_at = ""
    identity = re.search(
        rf"Summoner\(\s*({_JSON_STRING})\s*,\s*({_JSON_STRING})\s*,\s*"
        rf"({_JSON_STRING}|null)",
        text,
    )
    if identity:
        game_name = str(_decoded_token(identity.group(1)) or requested_game_name)
        source_updated_at = str(_decoded_token(identity.group(3)) or "")

    tier = "UNRANKED"
    division = ""
    league_points = season_wins = season_losses = 0
    solo = re.search(
        r'LeagueStat\(\s*"SOLORANKED"\s*,\s*TierInfo\('
        r"\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^)]+)\)\s*,"
        r"\s*([^,]+)\s*,\s*([^)]+)\)",
        text,
    )
    if solo:
        tier_value = _decoded_token(solo.group(1))
        division_value = _decoded_token(solo.group(2))
        lp_value = _decoded_token(solo.group(3))
        wins_value = _decoded_token(solo.group(4))
        losses_value = _decoded_token(solo.group(5))
        tier = str(tier_value or "UNRANKED").upper()
        division = str(division_value or "")
        league_points = max(0, int(lp_value or 0))
        season_wins = max(0, int(wins_value or 0))
        season_losses = max(0, int(losses_value or 0))

    champion_stats: list[OpggMcpChampionStat] = []
    champion_pattern = re.compile(
        rf"MyChampionStat\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
        rf"\s*(\d+)\s*,\s*({_JSON_STRING}|null)\s*\)"
    )
    for match in champion_pattern.finditer(text):
        champion_key, games, wins, losses = (
            max(0, int(match.group(index))) for index in range(1, 5)
        )
        # Upstream should satisfy play == win + lose. Keep the explicit play
        # count while preventing an impossible win/loss pair from reaching UI.
        if wins + losses > games:
            games = wins + losses
        champion_stats.append(OpggMcpChampionStat(
            champion_key=champion_key,
            champion_name=str(_decoded_token(match.group(5)) or ""),
            games=games,
            wins=wins,
            losses=losses,
        ))

    return OpggMcpSummonerProfile(
        riot_id=f"{requested_game_name}#{requested_tag_line}",
        game_name=game_name,
        tag_line=requested_tag_line,
        region=region.upper(),
        tier=tier,
        division=division,
        league_points=league_points,
        season_wins=season_wins,
        season_losses=season_losses,
        source_updated_at=source_updated_at,
        fetched_at=fetched_at or datetime.now().isoformat(timespec="seconds"),
        champion_stats=champion_stats,
        status="OK",
    )


def parse_summoner_matches_text(text: str) -> list[OpggMcpRecentMatch]:
    if not text or "LolListSummonerMatches" not in text:
        raise OpggMcpError("OP.GG MCP 최근 경기 응답 형식을 확인할 수 없습니다.")
    number = r"-?\d+(?:\.\d+)?|null"
    pattern = re.compile(
        rf"GameHistory\(\s*({_JSON_STRING})\s*,\s*({_JSON_STRING})\s*,"
        rf"\s*({_JSON_STRING})\s*,\s*\[Participant\(Summoner\("
        rf"\s*({_JSON_STRING}|null)\s*,\s*({_JSON_STRING}|null)\s*,"
        rf"\s*({_JSON_STRING}|null)\s*\)\s*,\s*(\d+)\s*,"
        rf"\s*({_JSON_STRING}|null)\s*,\s*({_JSON_STRING}|null)\s*,"
        rf"\s*Stats\(\s*({number})\s*,\s*({number})\s*,\s*({number})\s*,"
        rf"\s*({_JSON_STRING}|null)\s*,\s*({number})\s*,\s*({number})\s*\)"
        rf"\s*\)\s*\]\s*\)",
    )
    matches: list[OpggMcpRecentMatch] = []
    for found in pattern.finditer(text):
        score_value = _decoded_token(found.group(14))
        rank_value = _decoded_token(found.group(15))
        matches.append(OpggMcpRecentMatch(
            match_id=str(_decoded_token(found.group(1)) or ""),
            created_at=str(_decoded_token(found.group(2)) or ""),
            game_type=str(_decoded_token(found.group(3)) or ""),
            champion_key=max(0, int(found.group(7))),
            champion_name=str(_decoded_token(found.group(8)) or ""),
            position=str(_decoded_token(found.group(9)) or "UNKNOWN").upper(),
            kills=max(0, int(_decoded_token(found.group(10)) or 0)),
            deaths=max(0, int(_decoded_token(found.group(11)) or 0)),
            assists=max(0, int(_decoded_token(found.group(12)) or 0)),
            result=str(_decoded_token(found.group(13)) or "UNKNOWN").upper(),
            op_score=max(0.0, float(score_value or 0.0)),
            op_score_rank=max(0, int(rank_value or 0)),
        ))
    return matches


def mcp_champion_token(champion_id: str) -> str:
    """Convert Data Dragon IDs to OP.GG MCP's UPPER_SNAKE_CASE names."""
    aliases = {
        "Kaisa": "KAISA", "Belveth": "BEL_VETH", "Chogath": "CHO_GATH",
        "DrMundo": "DR_MUNDO", "JarvanIV": "JARVAN_IV", "Khazix": "KHA_ZIX",
        "KogMaw": "KOG_MAW", "KSante": "K_SANTE", "Leblanc": "LEBLANC",
        "MonkeyKing": "WUKONG", "Nunu": "NUNU_WILLUMP", "RekSai": "REK_SAI",
        "Renata": "RENATA_GLASC", "TahmKench": "TAHM_KENCH",
        "Velkoz": "VEL_KOZ",
    }
    if champion_id in aliases:
        return aliases[champion_id]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", champion_id)
    return words.replace("'", "").replace(" ", "_").upper()


def parse_champion_synergies_text(
    text: str,
    *,
    requested_champion_id: str,
    key_resolver: Callable[[int], tuple[str, str]] | None = None,
    fetched_at: str | None = None,
) -> OpggSynergySnapshot:
    if not text or "LolGetChampionSynergies" not in text:
        raise OpggMcpError("OP.GG MCP 챔피언 조합 응답 형식을 확인할 수 없습니다.")
    number = r"-?\d+(?:\.\d+)?|null"
    pattern = re.compile(
        rf"Synergie\(\s*(\d+)\s*,\s*({_JSON_STRING}|null)\s*,"
        rf"\s*({_JSON_STRING}|null)\s*,\s*(\d+)\s*,"
        rf"\s*({_JSON_STRING}|null)\s*,\s*({_JSON_STRING}|null)\s*,"
        rf"\s*({number})\s*,\s*({number})\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
        rf"\s*({number})\s*,\s*SynergyTierData\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*\)"
    )
    rows: list[OpggSynergyStat] = []
    ally_key = 0
    ally_name = ""
    ally_position = "BOTTOM"
    candidate_position = "SUPPORT"
    for found in pattern.finditer(text):
        ally_key = ally_key or max(0, int(found.group(1)))
        ally_name = ally_name or str(_decoded_token(found.group(2)) or "")
        source_position = str(_decoded_token(found.group(3)) or "ADC").upper()
        ally_position = "BOTTOM" if source_position == "ADC" else source_position
        champion_key = max(0, int(found.group(4)))
        response_name = str(_decoded_token(found.group(5)) or "")
        target_position = str(_decoded_token(found.group(6)) or "SUPPORT").upper()
        candidate_position = "BOTTOM" if target_position == "ADC" else target_position
        champion_id, registry_name = (
            key_resolver(champion_key) if key_resolver
            else (f"Champion{champion_key}", response_name)
        )
        raw_rate = float(_decoded_token(found.group(11)) or 0.0)
        win_rate = raw_rate * 100.0 if 0.0 <= raw_rate <= 1.0 else raw_rate
        rows.append(OpggSynergyStat(
            champion_key=champion_key,
            champion_id=champion_id,
            champion_name_ko=response_name or registry_name,
            games=max(0, int(found.group(9))),
            wins=max(0, int(found.group(10))),
            win_rate=round(win_rate, 2) if 0 <= win_rate <= 100 else None,
            synergy_rank=max(0, int(_decoded_token(found.group(7)) or 0)),
            synergy_tier=max(0, int(found.group(12))),
            tier_rank=max(0, int(found.group(13))),
        ))
    if not rows:
        raise OpggMcpError("OP.GG MCP에서 조합 통계를 찾지 못했습니다.")
    rows.sort(key=lambda item: item.synergy_rank or 999)
    return OpggSynergySnapshot(
        ally_champion_key=ally_key,
        ally_champion_id=requested_champion_id,
        ally_champion_name_ko=ally_name,
        ally_position=ally_position,
        candidate_position=candidate_position,
        fetched_at=fetched_at or datetime.now().isoformat(timespec="seconds"),
        synergies=rows,
        status="OK",
    )


class OpggMcpClient:
    """Small dependency-free Streamable HTTP MCP client for OP.GG."""

    def __init__(
        self,
        endpoint: str = OPGG_MCP_ENDPOINT,
        timeout: float = 15.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.session_id = ""
        self._request_id = 0

    @staticmethod
    def _decode_http_body(body: bytes) -> dict[str, Any] | None:
        text = body.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        if text.startswith("{"):
            return dict(json.loads(text))
        # Streamable HTTP may use SSE. Each event can contain several data lines.
        event_lines: list[str] = []
        decoded: dict[str, Any] | None = None
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                event_lines.append(line[5:].lstrip())
            elif not line.strip() and event_lines:
                candidate = "\n".join(event_lines)
                event_lines.clear()
                try:
                    value = json.loads(candidate)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    decoded = value
        return decoded

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "LoL-Support-Advisor/0.2",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id", "")
                if session_id:
                    self.session_id = str(session_id)
                return self._decode_http_body(response.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise OpggMcpError(f"OP.GG MCP HTTP {exc.code} · {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OpggMcpError(f"OP.GG MCP 연결 실패 · {exc}") from exc

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        })
        if not response:
            raise OpggMcpError("OP.GG MCP가 빈 응답을 반환했습니다.")
        if response.get("error"):
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise OpggMcpError(f"OP.GG MCP 오류 · {message}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise OpggMcpError("OP.GG MCP 결과 형식이 올바르지 않습니다.")
        return result

    def connect(self) -> None:
        if self.session_id:
            return
        result = self._request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "lol-support-advisor", "version": "0.2.0"},
        })
        if not self.session_id:
            raise OpggMcpError("OP.GG MCP 세션 ID를 받지 못했습니다.")
        if not result.get("serverInfo"):
            raise OpggMcpError("OP.GG MCP 서버 정보를 확인하지 못했습니다.")
        # Notifications have no response id and commonly return HTTP 202.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def summoner_profile(
        self,
        game_name: str,
        tag_line: str,
        region: str = "KR",
        lang: str = "ko_KR",
    ) -> OpggMcpSummonerProfile:
        self.connect()
        result = self._request("tools/call", {
            "name": "lol_get_summoner_profile",
            "arguments": {
                "game_name": game_name,
                "tag_line": tag_line,
                "region": region.upper(),
                "lang": lang,
                "desired_output_fields": [
                    "data.summoner.{game_name,tagline,updated_at}",
                    "data.summoner.league_stats[].{game_type,win,lose}",
                    "data.summoner.league_stats[].tier_info.{tier,division,lp}",
                    "data.summoner.ranked_most_champions.my_champion_stats[]."
                    "{id,play,win,lose,champion_name}",
                ],
            },
        })
        if result.get("isError"):
            raise OpggMcpError("OP.GG에서 소환사 프로필을 찾지 못했습니다.")
        texts = [
            str(item.get("text") or "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return parse_summoner_profile_text(
            "\n".join(texts),
            requested_game_name=game_name,
            requested_tag_line=tag_line,
            region=region,
        )

    def summoner_recent_matches(
        self,
        game_name: str,
        tag_line: str,
        region: str = "KR",
        lang: str = "ko_KR",
        limit: int = 10,
    ) -> list[OpggMcpRecentMatch]:
        self.connect()
        result = self._request("tools/call", {
            "name": "lol_list_summoner_matches",
            "arguments": {
                "game_name": game_name,
                "tag_line": tag_line,
                "region": region.upper(),
                "lang": lang,
                "limit": max(5, min(int(limit), 20)),
                "desired_output_fields": [
                    "data.game_history[].{created_at,game_type,id}",
                    "data.game_history[].participants[].summoner."
                    "{game_name,tagline,puuid}",
                    "data.game_history[].participants[]."
                    "{champion_id,champion_name,position}",
                    "data.game_history[].participants[].stats."
                    "{result,kill,death,assist,op_score,op_score_rank}",
                ],
            },
        })
        if result.get("isError"):
            raise OpggMcpError("OP.GG에서 최근 경기 기록을 찾지 못했습니다.")
        texts = [
            str(item.get("text") or "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return parse_summoner_matches_text("\n".join(texts))

    def champion_synergies(
        self,
        champion_id: str,
        *,
        my_position: str = "adc",
        synergy_position: str = "support",
        lang: str = "ko_KR",
        key_resolver: Callable[[int], tuple[str, str]] | None = None,
    ) -> OpggSynergySnapshot:
        self.connect()
        result = self._request("tools/call", {
            "name": "lol_get_champion_synergies",
            "arguments": {
                "champion": mcp_champion_token(champion_id),
                "my_position": my_position.lower(),
                "synergy_position": synergy_position.lower(),
                "lang": lang,
                "desired_output_fields": [
                    "champion", "my_position", "synergy_position",
                    "data.synergies[].{champion_id,champion_name,play,position,"
                    "score,score_rank,synergy_champion_id,synergy_champion_name,"
                    "synergy_position,win,win_rate}",
                    "data.synergies[].synergy_tier_data.{rank,tier}",
                ],
            },
        })
        if result.get("isError"):
            raise OpggMcpError("OP.GG에서 챔피언 조합 통계를 찾지 못했습니다.")
        texts = [
            str(item.get("text") or "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return parse_champion_synergies_text(
            "\n".join(texts), requested_champion_id=champion_id,
            key_resolver=key_resolver,
        )
