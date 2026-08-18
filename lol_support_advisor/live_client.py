from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .champions import ChampionRegistry
from .models import LiveGameSnapshot, LivePlayer


class LiveClientUnavailable(RuntimeError):
    pass


class LiveClient:
    def __init__(self, registry: ChampionRegistry, timeout: float = 2.0) -> None:
        self.registry = registry
        self.timeout = timeout

    def get(self, path: str) -> Any:
        request = Request(f"https://127.0.0.1:2999{path}")
        context = ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=self.timeout, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise LiveClientUnavailable("진행 중인 게임 데이터를 아직 읽을 수 없습니다.") from exc

    @staticmethod
    def _private_player_name(raw: dict[str, Any], index: int) -> str:
        team = str(raw.get("team") or "UNKNOWN").upper()
        position = str(raw.get("position") or "UNKNOWN").upper()
        return f"비공개 {team} {position} {index + 1}"

    def _snapshot_from_payloads(
        self,
        players_raw: list[dict[str, Any]],
        active_raw: dict[str, Any] | None = None,
        game_raw: dict[str, Any] | None = None,
    ) -> LiveGameSnapshot:
        active_raw = active_raw or {}
        game_raw = game_raw or {}
        active_riot_id = str(active_raw.get("riotId") or active_raw.get("summonerName") or "")
        active_game_name = str(active_raw.get("riotIdGameName") or active_raw.get("summonerName") or "")
        active_tag_line = str(active_raw.get("riotIdTagLine") or "")
        active_champion_id = self.registry.normalize_id(
            str(active_raw.get("championName") or "Unknown")
        )
        active_team = "ORDER"
        players: list[LivePlayer] = []
        champion_counts: dict[str, int] = {}
        for raw in players_raw:
            champion_id = self.registry.normalize_id(str(raw.get("championName") or "Unknown"))
            champion_counts[champion_id] = champion_counts.get(champion_id, 0) + 1
        for index, raw in enumerate(players_raw):
            game_name = str(raw.get("riotIdGameName") or raw.get("summonerName") or "")
            tag_line = str(raw.get("riotIdTagLine") or "")
            riot_id = str(raw.get("riotId") or "")
            if not tag_line and "#" in riot_id:
                game_name, tag_line = riot_id.rsplit("#", 1)
            champion_id = self.registry.normalize_id(str(raw.get("championName") or "Unknown"))
            if not game_name or not tag_line:
                game_name = self._private_player_name(raw, index)
                tag_line = ""
            player = LivePlayer(
                champion_id=champion_id,
                champion_name_ko=self.registry.ko_name(champion_id),
                riot_game_name=game_name,
                riot_tag_line=tag_line,
                team=str(raw.get("team") or "UNKNOWN"),
                position=str(raw.get("position") or "UNKNOWN"),
                level=int(raw.get("level") or 1),
                is_active_player=bool(riot_id and riot_id == active_riot_id)
                    or bool(
                        game_name and game_name == active_game_name
                        and (not active_tag_line or tag_line == active_tag_line)
                    )
                    or bool(
                        active_champion_id != "Unknown"
                        and champion_id == active_champion_id
                        and champion_counts.get(champion_id) == 1
                    ),
            )
            if player.is_active_player:
                active_team = player.team
            players.append(player)
        return LiveGameSnapshot(
            players=players,
            active_riot_id=active_riot_id,
            active_team=active_team,
            game_time=float(game_raw.get("gameTime") or 0),
            game_mode=str(game_raw.get("gameMode") or ""),
        )

    def identity_snapshot(self) -> LiveGameSnapshot:
        """Read the smallest endpoint during the brief loading-name window."""
        players_raw = self.get("/liveclientdata/playerlist")
        if not isinstance(players_raw, list):
            raise LiveClientUnavailable("현재 게임 명단을 읽을 수 없습니다.")
        return self._snapshot_from_payloads(players_raw)

    def snapshot(self) -> LiveGameSnapshot:
        # playerlist is materially smaller than allgamedata and contains every
        # player field this screen needs.
        players_raw = self.get("/liveclientdata/playerlist")
        active_raw = self.get("/liveclientdata/activeplayer")
        game_raw = self.get("/liveclientdata/gamestats")
        if not isinstance(players_raw, list):
            raise LiveClientUnavailable("현재 게임 명단을 읽을 수 없습니다.")
        return self._snapshot_from_payloads(
            players_raw,
            active_raw if isinstance(active_raw, dict) else {},
            game_raw if isinstance(game_raw, dict) else {},
        )
